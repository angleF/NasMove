from __future__ import annotations

from dataclasses import is_dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import QObject, Signal, Slot

from nasmove.core.states import TaskState
from nasmove.transfer.progress import ProgressSnapshot
from nasmove.ui.task_page import TaskPage
from nasmove.ui.task_presentation import safe_code

if TYPE_CHECKING:
    from nasmove.ui.queue_panel import QueuePanel

# Task states a normal transfer run can end in. A stored error on one of these
# is an item-level failure with an actionable reason; anything else means the
# task itself was stopped.
_OUTCOME_STATES = frozenset(
    {"completed", "completed_with_warnings", "failed", "canceled", "paused"}
)

# The strict subset of outcome states whose stored error is an item-level reason
# worth showing in place of the task-level stop message. ``paused``/``canceled``
# are deliberately absent: a failed pause/resume/cancel command records an
# execution-stop code without changing the row's TaskState, so routing those
# states here would hide the stop and its "some files may already have been
# transferred" safety prompt.
_ITEM_REASON_STATES = frozenset({"completed", "completed_with_warnings", "failed"})


class TaskController(QObject):
    """Connect thread-safe transfer notifications and task commands to ``TaskPage``."""

    _event_received = Signal(object)
    _progress_received = Signal(object)
    _result_received = Signal(object)
    _execution_error_received = Signal(object)
    event_applied = Signal()
    resume_execution = Signal()
    workspace_state_changed = Signal(object)
    task_state_updated = Signal(object, object)

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
        self._queue_panel: object | None = None
        self._tasks: tuple[object, ...] = ()
        self._active_id: object | None = None
        self._events: dict[object, object] = {}
        self._progress: dict[object, ProgressSnapshot] = {}
        self._results: dict[object, object] = {}
        self._errors: dict[object, str] = {}
        self._event_received.connect(self._apply_event)
        self._progress_received.connect(self._apply_progress)
        self._result_received.connect(self._apply_result)
        self._execution_error_received.connect(self._apply_execution_error)
        page.pause_requested.connect(lambda: self._dispatch("pause"))
        page.resume_requested.connect(lambda: self._dispatch("resume"))
        page.cancel_requested.connect(lambda: self._dispatch("cancel"))
        page.delete_requested.connect(lambda: self._dispatch("delete"))
        page.queue_reordered.connect(self._persist_order)
        page.selection_changed.connect(self._select)
        page.files_requested.connect(self._show_files)

    def attach_queue_panel(self, panel: QueuePanel) -> None:
        """Keep the compact queue and the existing detail page on one task state."""
        self._queue_panel = panel
        panel.task_selected.connect(self._select)
        panel.pause_requested.connect(lambda task_id: self._dispatch_for(task_id, "pause"))
        panel.resume_requested.connect(lambda task_id: self._dispatch_for(task_id, "resume"))
        panel.cancel_requested.connect(lambda task_id: self._dispatch_for(task_id, "cancel"))
        panel.delete_requested.connect(lambda task_id: self._dispatch_for(task_id, "delete"))
        panel.move_to_top_requested.connect(self._move_to_top)
        panel.set_tasks(self._tasks)

    def _show_files(self) -> None:
        from PySide6.QtWidgets import (
            QDialog,
            QLabel,
            QPushButton,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout,
        )
        reader = getattr(self._repository, "list_item_page", None)
        dialog = QDialog(self._page)
        dialog.setWindowTitle("文件处理结果")
        dialog.resize(850, 480)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["源文件", "NAS 目标文件", "处理结果"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(QLabel("按记录显示处理结果；未完成文件不代表源文件已移入废纸篓。"))
        layout.addWidget(table)
        more = QPushButton("加载更多文件")
        layout.addWidget(more)
        task_id = self._page.selected_task_id()
        names = {"done": "完成", "committed": "目标已提交", "source_retained": "源文件保留",
            "verify_failed": "校验失败", "verified": "已校验", "transferring": "复制中",
            "transferred": "复制完成，等待校验", "verifying": "校验中", "interrupted": "需恢复",
            "planned": "等待处理", "waiting_retry": "等待重试", "source_changed": "源文件已变化",
            "source_delete_authorized": "移入废纸篓待确认", "skipped": "已跳过"}

        def load() -> None:
            if not callable(reader):
                more.setText("文件明细暂不可用")
                more.setEnabled(False)
                return
            try:
                items = reader(task_id, table.rowCount())
                for item in items:
                    row = table.rowCount()
                    table.insertRow(row)
                    for col, value in enumerate((str(item.source_path), str(item.final_path), names.get(item.state.value, "需要核对"))):
                        table.setItem(row, col, QTableWidgetItem(value))
                table.resizeColumnsToContents()
                more.setEnabled(len(items) == 100)
                more.setText("加载更多文件" if len(items) == 100 else "已显示全部文件")
            except Exception:  # noqa: BLE001 - read failure is visible in the dialog
                more.setText("无法读取文件记录，请稍后重试")
        more.clicked.connect(load)
        load()
        dialog.exec()

    def load_queue(self, tasks: tuple[object, ...]) -> None:
        self._tasks = tasks
        reader = getattr(self._repository, "last_ui_error", None)
        if callable(reader):
            for task in tasks:
                code = reader(getattr(task, "id", None))
                if code is not None and getattr(getattr(task, "state", None), "value", "") not in {"completed", "completed_with_warnings"}:
                    self._errors[getattr(task, "id", None)] = safe_code(code)
        self._page.set_queue(tasks)
        if self._queue_panel is not None:
            cast(Any, self._queue_panel).set_tasks(tasks)

    def append_task(self, task: object) -> None:
        task_id = getattr(task, "id", None)
        self._tasks = tuple(
            existing for existing in self._tasks if getattr(existing, "id", None) != task_id
        ) + (task,)
        self._page.set_queue(self._tasks)
        self._page.queue_list.setCurrentRow(self._page.queue_list.count() - 1)
        if self._queue_panel is not None:
            cast(Any, self._queue_panel).set_tasks(self._tasks)
            cast(Any, self._queue_panel).select_task(task_id)

    def publish(self, event: object) -> None:
        self._active_id = getattr(event, "task_id", self._active_id)
        self._event_received.emit(event)

    def publish_progress(self, snapshot: ProgressSnapshot) -> None:
        self._progress_received.emit((self._active_id, snapshot))

    def publish_result(self, result: object) -> None:
        self._result_received.emit((getattr(result, "task_id", None) or self._active_id, result))

    def publish_execution_error(self, failure: object = None) -> None:
        self._execution_error_received.emit(failure or (self._active_id, "unexpected_error"))

    @Slot(object)
    def _apply_event(self, event: object) -> None:
        task_id = getattr(event, "task_id", None)
        if getattr(getattr(event, "state", None), "value", "") in {"running", "queued"}:
            self._results.pop(task_id, None)
            self._errors.pop(task_id, None)
        self._events[task_id] = event
        self._update_row(task_id, getattr(event, "state", None))
        if self._queue_panel is not None:
            cast(Any, self._queue_panel).apply_event(event)
        if self._visible(task_id):
            self._page.apply_event(event)
            self.workspace_state_changed.emit(getattr(event, "state", None))
            item_id = getattr(event, "item_id", None)
            getter = getattr(self._repository, "get_item", None)
            if item_id is not None and callable(getter):
                try:
                    item = getter(item_id)
                    self._page.current_file_label.setText(f"当前文件：{item.source_path}")
                except Exception:  # noqa: BLE001 - optional UI file detail
                    self._page.current_file_label.setText("当前文件：文件信息暂不可用")
        self.event_applied.emit()

    @Slot(object)
    def _apply_progress(self, snapshot: object) -> None:
        task_id, snapshot = cast(tuple[object, object], snapshot)
        if isinstance(snapshot, ProgressSnapshot):
            self._progress[task_id] = snapshot
            if self._queue_panel is not None:
                cast(Any, self._queue_panel).apply_progress(task_id, snapshot)
            if self._visible(task_id) and task_id not in self._results and task_id not in self._errors:
                state = getattr(self._events.get(task_id), "state", None)
                self._page.apply_snapshot(snapshot, state=state)

    @Slot(object)
    def _apply_result(self, result: object) -> None:
        task_id, result = cast(tuple[object, object], result)
        self._results[task_id] = result
        self._errors.pop(task_id, None)
        self._update_row(task_id, getattr(result, "state", None))
        if self._queue_panel is not None:
            cast(Any, self._queue_panel).apply_result(result, task_id=task_id)
        if self._visible(task_id):
            self._sync_task_summary(task_id)
            self._page.show_result(result)
            self._sync_deletion_outcome(task_id)
            self._sync_task_summary(task_id)
        error = getattr(result, "error", None)
        recorder = getattr(self._repository, "record_ui_error", None)
        if error is not None and callable(recorder):
            from nasmove.smb.error_mapping import redacted_error_code
            try:
                recorder(task_id, safe_code(redacted_error_code(error)))
            except Exception:  # noqa: BLE001 - persistence status must be visible
                if self._visible(task_id):
                    self._page.error_details.append("错误记录未保存，请导出报告。")

    @Slot(object)
    def _apply_execution_error(self, failure: object) -> None:
        task_id, code = cast(tuple[object, str], failure)
        code = safe_code(code)
        self._errors[task_id] = code
        self._update_row(task_id, "execution_stopped")
        recorder = getattr(self._repository, "record_ui_error", None)
        saved = False
        if callable(recorder):
            try:
                recorder(task_id, code)
                saved = True
            except Exception:  # noqa: BLE001 - report failed persistence below
                saved = False
        if self._visible(task_id) or task_id is None:
            self._sync_task_summary(task_id)
            self._page.show_execution_error(code)
            self._page.error_details.append("错误记录已保存。" if saved else "错误记录未保存，请导出报告。")

    def _visible(self, task_id: object) -> bool:
        return self._page.selected_task_id() == task_id or not self._tasks

    def _update_row(self, task_id: object, state: object) -> None:
        from PySide6.QtCore import Qt
        if isinstance(state, TaskState):
            self._tasks = tuple(
                replace(cast(Any, task), state=state)
                if getattr(task, "id", None) == task_id and is_dataclass(task) else task
                for task in self._tasks
            )
        self.task_state_updated.emit(task_id, state)
        for index in range(self._page.queue_list.count()):
            item = self._page.queue_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == task_id:
                item.setData(Qt.ItemDataRole.UserRole + 1, str(getattr(state, "value", state)))
                item.setText(item.text().rsplit("  [", 1)[0] + f"  [{self._page._state_text(state)}]")

    @Slot(object)
    def _select(self, task_id: object) -> None:
        task = next((task for task in self._tasks if getattr(task, "id", None) == task_id), None)
        if task is None:
            return
        self._page.show_task(task)
        self._sync_task_summary(task_id)
        if task_id in self._events:
            self._page.apply_event(self._events[task_id])
        if task_id in self._progress:
            self._page.apply_snapshot(self._progress[task_id], state=getattr(self._events.get(task_id), "state", getattr(task, "state", None)))
        elif getattr(self._page, "_task_summary", None) is not None:
            summary = self._page._task_summary
            confirmed = int(getattr(summary, "confirmed_bytes", 0))
            total_bytes = int(getattr(summary, "total_bytes", 0) or getattr(task, "total_bytes", 0))
            verified_count = int(getattr(summary, "verified_items", 0))
            total_count = int(getattr(summary, "total_items", 0) or getattr(task, "total_files", 0))
            if confirmed > 0 and total_bytes > 0:
                verified_bytes = total_bytes if (total_count > 0 and verified_count >= total_count) else 0
                self._page.apply_snapshot(
                    ProgressSnapshot(
                        total_bytes * 2,
                        0,
                        confirmed,
                        verified_bytes,
                        0,
                        None,
                    ),
                    state=getattr(self._events.get(task_id), "state", getattr(task, "state", None)),
                )
        if task_id in self._results:
            self._page.show_result(self._results[task_id])
        elif str(getattr(getattr(task, "state", None), "value", "")) in _OUTCOME_STATES:
            from nasmove.core.states import TaskState
            from nasmove.transfer.transfer_engine import TaskResult
            state = cast(Any, task).state
            self._page.show_result(TaskResult(state is TaskState.COMPLETED, state))
        if task_id in self._errors:
            if str(getattr(getattr(task, "state", None), "value", "")) in _ITEM_REASON_STATES:
                # A task that finished on its own keeps the item-level, readable
                # reason; a stopped or merely paused task keeps the task-level
                # stop message and its safety prompt.
                self._page._show_reason(self._errors[task_id])
            else:
                self._page.show_execution_error(self._errors[task_id])
        self._sync_deletion_outcome(task_id)
        self._sync_task_summary(task_id)

    def _sync_task_summary(self, task_id: object) -> None:
        """Forward aggregate task transfer statistics to the task view."""
        reader = getattr(self._repository, "task_summary", None)
        if not callable(reader) or task_id is None:
            return
        try:
            summary = reader(task_id)
        except Exception:  # noqa: BLE001 - diagnostics must not break the view
            summary = None
        self._page.set_task_summary(summary)

    def _sync_deletion_outcome(self, task_id: object) -> None:
        """Give the persisted source-deletion outcome a production reader.

        Defect A's goal was that a retained source has a retrievable reason, so
        the page's exported summary must show it rather than requiring someone to
        open SQLite by hand.
        """
        reader = getattr(self._repository, "last_deletion_outcome_for_task", None)
        if not callable(reader):
            return
        try:
            outcome = reader(task_id)
        except Exception:  # noqa: BLE001 - diagnostics must not break the view
            outcome = None
        self._page.set_deletion_outcome(None if outcome is None else str(outcome[0]))

    def _dispatch(self, name: str) -> None:
        task_id = self._page.selected_task_id()
        self._dispatch_for(task_id, name)

    def _dispatch_for(self, task_id: object, name: str) -> None:
        if task_id is None or self._commands is None:
            return
        command = getattr(self._commands, name, None)
        if callable(command):
            try:
                cast(Any, command)(task_id)
                if name == "delete":
                    self._tasks = tuple(t for t in self._tasks if getattr(t, "id", None) != task_id)
                    self._events.pop(task_id, None)
                    self._results.pop(task_id, None)
                    self._errors.pop(task_id, None)
                    if self._queue_panel is not None:
                        cast(Any, self._queue_panel).set_tasks(self._tasks)
                    self._page.set_queue(self._tasks)
                    return
                getter = getattr(self._repository, "get_task", None)
                if callable(getter):
                    fresh = getter(task_id)
                    self._tasks = tuple(fresh if getattr(t, "id", None) == task_id else t for t in self._tasks)
                    self._events.pop(task_id, None)
                    self._results.pop(task_id, None)
                    self._errors.pop(task_id, None)
                    self._select(task_id)
                    self._update_row(task_id, fresh.state)
                    if self._queue_panel is not None:
                        cast(Any, self._queue_panel).set_tasks(self._tasks)
                        cast(Any, self._queue_panel).select_task(task_id)
                if name == "resume":
                    self.resume_execution.emit()
            except Exception as error:  # noqa: BLE001 - command errors are user-visible
                from nasmove.smb.error_mapping import redacted_error_code
                self._apply_execution_error((task_id, redacted_error_code(error)))

    @Slot(object)
    def _move_to_top(self, task_id: object) -> None:
        queued = [task for task in self._tasks if getattr(task, "state", None) is TaskState.QUEUED]
        selected = next((task for task in queued if getattr(task, "id", None) == task_id), None)
        if selected is None:
            return
        queued = [selected, *(task for task in queued if task is not selected)]
        queued_iter = iter(queued)
        reordered = tuple(
            next(queued_iter) if getattr(task, "state", None) is TaskState.QUEUED else task
            for task in self._tasks
        )
        self._persist_order(tuple(getattr(task, "id", None) for task in queued))
        self._tasks = reordered
        self._page.set_queue(self._tasks)
        if self._queue_panel is not None:
            cast(Any, self._queue_panel).set_tasks(self._tasks)
            cast(Any, self._queue_panel).select_task(task_id)

    @Slot(object)
    def _persist_order(self, task_ids: object) -> None:
        if self._repository is None or not isinstance(task_ids, tuple):
            return
        reorder = getattr(self._repository, "reorder_queued_tasks", None)
        if callable(reorder):
            try:
                getter = getattr(self._repository, "get_task", None)
                queued_ids = task_ids if not callable(getter) else tuple(
                    task_id for task_id in task_ids if getter(task_id).state.value == "queued"
                )
                cast(Any, reorder)(queued_ids)
            except Exception:  # noqa: BLE001 - restore visible order after a rejected reorder
                self._page.set_queue(self._tasks)
                self._page.result_summary.setText("未能调整顺序：只能调整尚未开始的排队任务。")


__all__ = ["TaskController"]
