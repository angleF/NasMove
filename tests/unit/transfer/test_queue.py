import time
from threading import Event, Lock, Thread, get_ident

import pytest


def _gate_publication(coordinator: object, gate: Event, main_thread_id: int) -> None:
    """Park a worker between ``_run_lock`` acquisition and token publication.

    The first ``_lifecycle_lock`` entry from a non-main thread (the worker's
    publication block in ``run_next``) blocks on ``gate`` before acquiring the
    inner lock, so lifecycle calls from the main thread land inside the
    publication window deterministically.
    """

    class GatedLifecycleLock:
        def __init__(self, inner: Lock) -> None:
            self._inner = inner
            self._armed = True

        def __enter__(self) -> Lock:
            if self._armed and get_ident() != main_thread_id:
                self._armed = False
                gate.wait(1)
            return self._inner.__enter__()

        def __exit__(self, *exc_info: object) -> None:
            self._inner.__exit__(*exc_info)

    coordinator._lifecycle_lock = GatedLifecycleLock(coordinator._lifecycle_lock)


def _wait_for_run_lock(coordinator: object, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not coordinator._run_lock.locked():
        assert time.monotonic() < deadline, "worker never acquired the run lock"
        time.sleep(0.001)


def test_queue_never_runs_two_tasks_at_once(queue_fixture) -> None:
    queue_fixture.enqueue("task-a")
    queue_fixture.enqueue("task-b")
    queue_fixture.run_until_empty()

    assert queue_fixture.max_concurrent_tasks == 1
    assert queue_fixture.completed_order == ["task-a", "task-b"]


def test_empty_queue_can_be_polled_repeatedly(queue_fixture) -> None:
    assert queue_fixture.coordinator.run_next() is None
    assert queue_fixture.coordinator.run_next() is None


def test_repository_lookup_failure_does_not_leak_queue_lock() -> None:
    from nasmove.core.states import TaskState
    from nasmove.transfer.transfer_engine import QueueCoordinator, TaskResult

    class Repository:
        def __init__(self) -> None:
            self.fail = True

        def next_queued_task(self):
            if self.fail:
                self.fail = False
                raise OSError("database unavailable")

    class Engine:
        def run_task(self, task_id, token):
            del task_id, token
            return TaskResult(True, TaskState.COMPLETED)

    repository = Repository()
    coordinator = QueueCoordinator(Engine(), repository)
    with pytest.raises(OSError):
        coordinator.run_next()
    assert coordinator.run_next() is None


def test_engine_failure_does_not_leak_queue_lock() -> None:
    from nasmove.core.states import TaskState
    from nasmove.transfer.transfer_engine import QueueCoordinator, TaskResult

    class Engine:
        def __init__(self) -> None:
            self.fail = True

        def run_task(self, task_id, token):
            del task_id, token
            if self.fail:
                self.fail = False
                raise OSError("transfer failed")
            return TaskResult(True, TaskState.COMPLETED)

    engine = Engine()
    coordinator = QueueCoordinator(engine)
    coordinator.enqueue("task-a")
    with pytest.raises(OSError):
        coordinator.run_next()
    coordinator.enqueue("task-b")
    assert coordinator.run_next() is not None


def test_queue_pause_waits_for_running_engine_to_reach_safe_boundary() -> None:
    from nasmove.core.states import TaskState
    from nasmove.transfer.transfer_engine import QueueCoordinator, TaskResult

    started = Event()
    release = Event()

    class Engine:
        def run_task(self, task_id, token):
            del task_id
            started.set()
            while not token.pause_requested:
                release.wait(0.001)
            release.wait()
            return TaskResult(False, TaskState.PAUSED)

    coordinator = QueueCoordinator(Engine())
    coordinator.enqueue("task-a")
    worker = Thread(target=coordinator.run_next)
    worker.start()
    assert started.wait(1)

    coordinator.stop_accepting()
    coordinator.request_pause()
    assert coordinator.wait_for_safe_boundary(0.001) is False
    release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert coordinator.wait_for_safe_boundary(1) is True

    with pytest.raises(RuntimeError, match="stopping"):
        coordinator.enqueue("task-b")


def test_shutdown_observes_task_selection_as_active_and_passes_pause_token() -> None:
    from nasmove.core.model import TaskId
    from nasmove.core.states import TaskState
    from nasmove.transfer.transfer_engine import QueueCoordinator, TaskResult

    selection_started = Event()
    release_selection = Event()
    engine_started = Event()
    received: list[object] = []

    class Repository:
        def next_queued_task(self):
            selection_started.set()
            release_selection.wait(1)
            return type("Task", (), {"id": TaskId("selected")})()

    class Engine:
        def run_task(self, task_id, token):
            received.append(token)
            engine_started.set()
            return TaskResult(False, TaskState.PAUSED)

    coordinator = QueueCoordinator(Engine(), Repository())
    worker = Thread(target=coordinator.run_next)
    worker.start()
    assert selection_started.wait(1)

    coordinator.stop_accepting()
    coordinator.request_pause()
    assert coordinator.wait_for_safe_boundary(0.01) is False
    release_selection.set()
    assert engine_started.wait(1)
    worker.join(1)
    assert not worker.is_alive()
    assert received and received[0].pause_requested is True
    assert coordinator.wait_for_safe_boundary(1) is True


def test_stopping_queue_does_not_start_task_published_after_shutdown_window() -> None:
    from nasmove.core.states import TaskState
    from nasmove.transfer.transfer_engine import QueueCoordinator, TaskResult

    received: list[object] = []
    results: list[object] = []

    class Engine:
        def run_task(self, task_id, token):
            del task_id
            received.append(token)
            return TaskResult(False, TaskState.PAUSED)

    coordinator = QueueCoordinator(Engine())
    coordinator.enqueue("task-a")
    gate = Event()
    _gate_publication(coordinator, gate, get_ident())

    worker = Thread(target=lambda: results.append(coordinator.run_next()))
    worker.start()
    _wait_for_run_lock(coordinator)

    # Shutdown completes while the worker is parked between acquiring the run
    # lock and publishing its token: no active boundary is visible yet.
    coordinator.stop_accepting()
    coordinator.request_pause()
    assert coordinator.wait_for_safe_boundary(0.001) is True

    gate.set()
    worker.join(1)
    assert not worker.is_alive()
    assert results == [None]
    assert received == []


def test_pause_requested_before_publication_is_latched_onto_published_token() -> None:
    from nasmove.core.states import TaskState
    from nasmove.transfer.transfer_engine import QueueCoordinator, TaskResult

    received: list[object] = []
    results: list[object] = []

    class Engine:
        def run_task(self, task_id, token):
            del task_id
            received.append(token)
            return TaskResult(False, TaskState.PAUSED)

    coordinator = QueueCoordinator(Engine())
    coordinator.enqueue("task-a")
    gate = Event()
    _gate_publication(coordinator, gate, get_ident())

    worker = Thread(target=lambda: results.append(coordinator.run_next()))
    worker.start()
    _wait_for_run_lock(coordinator)

    # Pause arrives while no token is published, so the safe-boundary wait
    # alone cannot observe it; the request must latch onto the future token.
    coordinator.request_pause()
    assert coordinator.wait_for_safe_boundary(0.001) is True

    gate.set()
    worker.join(1)
    assert not worker.is_alive()
    assert received and received[0].pause_requested is True
    assert results and results[0].state is TaskState.PAUSED
    assert coordinator.wait_for_safe_boundary(1) is True
