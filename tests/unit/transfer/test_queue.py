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
