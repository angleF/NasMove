from __future__ import annotations

from dataclasses import is_dataclass, replace
from typing import Any, cast

from PySide6.QtCore import QObject, Signal, Slot

from nasmove.core.states import TaskState
from nasmove.transfer.progress import ProgressSnapshot
from nasmove.ui.task_page import TaskPage
from nasmove.ui.task_presentation import safe_code


class TaskController(QObject):
    """Connect thread-safe transfer notifications and task commands to ``TaskPage``."""

    _event_received = Signal(object)
    _progress_received = Signal(object)
    _result_received = Signal(object)
    _execution_error_received = Signal(object)
    event_applied = Signal()
    resume_execution = Signal()
    workspace_state_changed = Signal(object)

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
        page.queue_reordered.connect(self._persist_order)
        page.selection_changed.connect(self._select)
        page.files_requested.connect(self._show_files)

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
        layout.addWidget(QLabel("按记录显示处理结果；未完成文件不代表源文件已删除。"))
        layout.addWidget(table)
        more = QPushButton("加载更多文件")
        layout.addWidget(more)
        task_id = self._page.selected_task_id()
        names = {"done": "完成", "committed": "目标已提交", "source_retained": "源文件保留",
            "verify_failed": "校验失败", "verified": "已校验", "transferring": "复制中",
            "transferred": "复制完成，等待校验", "verifying": "校验中", "interrupted": "需恢复",
            "planned": "等待处理", "waiting_retry": "等待重试", "source_changed": "源文件已变化",
            "source_delete_authorized": "删除源文件待确认", "skipped": "已跳过"}

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

    def append_task(self, task: object) -> None:
        task_id = getattr(task, "id", None)
        self._tasks = tuple(
            existing for existing in self._tasks if getattr(existing, "id", None) != task_id
        ) + (task,)
        self._page.set_queue(self._tasks)
        self._page.queue_list.setCurrentRow(self._page.queue_list.count() - 1)

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
            if self._visible(task_id) and task_id not in self._results and task_id not in self._errors:
                state = getattr(self._events.get(task_id), "state", None)
                self._page.apply_snapshot(snapshot, state=state)

    @Slot(object)
    def _apply_result(self, result: object) -> None:
        task_id, result = cast(tuple[object, object], result)
        self._results[task_id] = result
        self._errors.pop(task_id, None)
        self._update_row(task_id, getattr(result, "state", None))
        if self._visible(task_id):
            self._page.show_result(result)
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
        if task_id in self._events:
            self._page.apply_event(self._events[task_id])
        if task_id in self._progress:
            self._page.apply_snapshot(self._progress[task_id], state=getattr(self._events.get(task_id), "state", getattr(task, "state", None)))
        if task_id in self._results:
            self._page.show_result(self._results[task_id])
        elif str(getattr(getattr(task, "state", None), "value", "")) in {"completed", "completed_with_warnings", "failed", "canceled", "paused"}:
            from nasmove.core.states import TaskState
            from nasmove.transfer.transfer_engine import TaskResult
            state = cast(Any, task).state
            self._page.show_result(TaskResult(state is TaskState.COMPLETED, state))
        if task_id in self._errors:
            self._page.show_execution_error(self._errors[task_id])

    def _dispatch(self, name: str) -> None:
        task_id = self._page.selected_task_id()
        if task_id is None or self._commands is None:
            return
        command = getattr(self._commands, name, None)
        if callable(command):
            try:
                cast(Any, command)(task_id)
                getter = getattr(self._repository, "get_task", None)
                if callable(getter):
                    fresh = getter(task_id)
                    self._tasks = tuple(fresh if getattr(t, "id", None) == task_id else t for t in self._tasks)
                    self._events.pop(task_id, None)
                    self._results.pop(task_id, None)
                    self._errors.pop(task_id, None)
                    self._select(task_id)
                    self._update_row(task_id, fresh.state)
                if name == "resume":
                    self.resume_execution.emit()
            except Exception as error:  # noqa: BLE001 - command errors are user-visible
                from nasmove.smb.error_mapping import redacted_error_code
                self._apply_execution_error((task_id, redacted_error_code(error)))

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
