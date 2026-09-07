from __future__ import annotations

from typing import Any, cast

from nasmove.core.states import TaskState


class TaskCommandService:
    """Translate UI task commands into durable state changes or cooperative tokens."""

    def __init__(self, repository: object, queue: object) -> None:
        self._repository = repository
        self._queue = queue

    def pause(self, task_id: object) -> None:
        task = self._task(task_id)
        if task.state in {TaskState.QUEUED, TaskState.INTERRUPTED, TaskState.WAITING_FOR_NETWORK}:
            self._transition(task, TaskState.PAUSED)
        elif task.state is TaskState.RUNNING:
            cast(Any, self._queue).request_pause()

    def resume(self, task_id: object) -> None:
        task = self._task(task_id)
        if task.state is not TaskState.PAUSED:
            return
        self._transition(task, TaskState.QUEUED)
        cast(Any, self._queue).enqueue(task.id)

    def cancel(self, task_id: object) -> None:
        task = self._task(task_id)
        if task.state in {
            TaskState.DRAFT,
            TaskState.PREFLIGHT,
            TaskState.QUEUED,
            TaskState.INTERRUPTED,
            TaskState.WAITING_FOR_NETWORK,
            TaskState.PAUSED,
        }:
            self._transition(task, TaskState.CANCELED)
        elif task.state is TaskState.RUNNING:
            cast(Any, self._queue).request_cancel()

    def _task(self, task_id: object) -> Any:
        return cast(Any, self._repository).get_task(task_id)

    def _transition(self, task: Any, target: TaskState) -> None:
        cast(Any, self._repository).transition_task(task.id, task.state, target)


__all__ = ["TaskCommandService"]
