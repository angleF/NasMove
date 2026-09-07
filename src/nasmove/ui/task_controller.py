from __future__ import annotations

from typing import Any, cast

from PySide6.QtCore import QObject, Signal, Slot

from nasmove.transfer.progress import ProgressSnapshot
from nasmove.ui.task_page import TaskPage


class TaskController(QObject):
    """Connect thread-safe transfer notifications and task commands to ``TaskPage``."""

    _event_received = Signal(object)
    _progress_received = Signal(object)
    _result_received = Signal(object)
    _execution_error_received = Signal()
    event_applied = Signal()

    def __init__(
        self,
        page: TaskPage,
        *,
        commands: object | None = None,
        repository: object | None = None,
    ) -> None:
        super().__init__(page)
        self._page = page
        self._commands = commands
        self._repository = repository
        self._tasks: tuple[object, ...] = ()
        self._event_received.connect(self._apply_event)
        self._progress_received.connect(self._apply_progress)
        self._result_received.connect(self._apply_result)
        self._execution_error_received.connect(self._apply_execution_error)
        page.pause_requested.connect(lambda: self._dispatch("pause"))
        page.resume_requested.connect(lambda: self._dispatch("resume"))
        page.cancel_requested.connect(lambda: self._dispatch("cancel"))
        page.queue_reordered.connect(self._persist_order)

    def load_queue(self, tasks: tuple[object, ...]) -> None:
        self._tasks = tasks
        self._page.set_queue(tasks)

    def append_task(self, task: object) -> None:
        task_id = getattr(task, "id", None)
        self._tasks = tuple(
            existing for existing in self._tasks if getattr(existing, "id", None) != task_id
        ) + (task,)
        self._page.set_queue(self._tasks)
        self._page.queue_list.setCurrentRow(self._page.queue_list.count() - 1)

    def publish(self, event: object) -> None:
        self._event_received.emit(event)

    def publish_progress(self, snapshot: ProgressSnapshot) -> None:
        self._progress_received.emit(snapshot)

    def publish_result(self, result: object) -> None:
        self._result_received.emit(result)

    def publish_execution_error(self) -> None:
        self._execution_error_received.emit()

    @Slot(object)
    def _apply_event(self, event: object) -> None:
        self._page.apply_event(event)
        self.event_applied.emit()

    @Slot(object)
    def _apply_progress(self, snapshot: object) -> None:
        if isinstance(snapshot, ProgressSnapshot):
            self._page.apply_snapshot(snapshot)

    @Slot(object)
    def _apply_result(self, result: object) -> None:
        self._page.show_result(result)

    @Slot()
    def _apply_execution_error(self) -> None:
        self._page.show_execution_error()

    def _dispatch(self, name: str) -> None:
        task_id = self._page.selected_task_id()
        if task_id is None or self._commands is None:
            return
        command = getattr(self._commands, name, None)
        if callable(command):
            cast(Any, command)(task_id)

    @Slot(object)
    def _persist_order(self, task_ids: object) -> None:
        if self._repository is None or not isinstance(task_ids, tuple):
            return
        reorder = getattr(self._repository, "reorder_queued_tasks", None)
        if callable(reorder):
            cast(Any, reorder)(task_ids)


__all__ = ["TaskController"]
