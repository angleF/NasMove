from __future__ import annotations

from dataclasses import replace

from nasmove.core.states import TaskState
from nasmove.ui.task_commands import TaskCommandService
from tests.fixtures.builders import build_task_record


class Repository:
    def __init__(self, state: TaskState) -> None:
        self.task = replace(build_task_record(), state=state)
        self.transitions: list[tuple[object, TaskState, TaskState]] = []

    def get_task(self, task_id: object) -> object:
        assert task_id == self.task.id
        return self.task

    def transition_task(self, task_id: object, expected: TaskState, target: TaskState) -> None:
        self.transitions.append((task_id, expected, target))
        self.task = replace(self.task, state=target)


class Queue:
    def __init__(self) -> None:
        self.enqueued: list[object] = []
        self.pause_count = 0
        self.cancel_count = 0

    def enqueue(self, task_id: object) -> None:
        self.enqueued.append(task_id)

    def request_pause(self) -> None:
        self.pause_count += 1

    def request_cancel(self) -> None:
        self.cancel_count += 1


def test_queued_task_can_be_paused_without_starting_engine() -> None:
    repository = Repository(TaskState.QUEUED)
    queue = Queue()
    service = TaskCommandService(repository, queue)

    service.pause(repository.task.id)

    assert repository.task.state is TaskState.PAUSED
    assert queue.pause_count == 0


def test_paused_task_resumes_by_transitioning_then_enqueueing() -> None:
    repository = Repository(TaskState.PAUSED)
    queue = Queue()
    service = TaskCommandService(repository, queue)

    service.resume(repository.task.id)

    assert repository.task.state is TaskState.QUEUED
    assert queue.enqueued == [repository.task.id]


def test_active_task_pause_and_cancel_use_cooperative_token() -> None:
    repository = Repository(TaskState.RUNNING)
    queue = Queue()
    service = TaskCommandService(repository, queue)

    service.pause(repository.task.id)
    service.cancel(repository.task.id)

    assert queue.pause_count == 1
    assert queue.cancel_count == 1


def test_waiting_task_cancel_is_persisted_without_running_engine() -> None:
    repository = Repository(TaskState.QUEUED)
    queue = Queue()
    service = TaskCommandService(repository, queue)

    service.cancel(repository.task.id)

    assert repository.task.state is TaskState.CANCELED
    assert queue.cancel_count == 0


def test_failed_task_can_be_resumed_and_enqueued() -> None:
    repository = Repository(TaskState.FAILED)
    queue = Queue()
    service = TaskCommandService(repository, queue)

    service.resume(repository.task.id)

    assert repository.task.state is TaskState.QUEUED
    assert queue.enqueued == [repository.task.id]


def test_failed_task_can_be_canceled() -> None:
    repository = Repository(TaskState.FAILED)
    queue = Queue()
    service = TaskCommandService(repository, queue)

    service.cancel(repository.task.id)

    assert repository.task.state is TaskState.CANCELED
    assert queue.cancel_count == 0
