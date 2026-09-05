from threading import Event, Thread

import pytest


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
