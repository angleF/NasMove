from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from nasmove.core.states import TaskState
from nasmove.transfer.progress import ProgressSnapshot
from nasmove.transfer.transfer_engine import TaskResult


class TaskPage(QWidget):
    pause_requested = Signal()
    resume_requested = Signal()
    cancel_requested = Signal()
    queue_reordered = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.copy_progress = QProgressBar()
        self.verify_progress = QProgressBar()
        self.status_label = QLabel("未开始")
        self.current_file_label = QLabel("当前文件：—")
        self.speed_label = QLabel("速度：—")
        self.eta_label = QLabel("剩余：—")
        self.result_summary = QLabel()
        self.error_details = QTextEdit()
        self.error_details.setReadOnly(True)
        self.pause_button = QPushButton("暂停")
        self.resume_button = QPushButton("继续")
        self.cancel_button = QPushButton("取消")
        self._update_actions(None)
        self.export_button = QPushButton("导出脱敏摘要")
        self.queue_list = QListWidget()
        self.move_up_button = QPushButton("上移")
        self.move_down_button = QPushButton("下移")
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.resume_button.clicked.connect(self.resume_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.export_button.clicked.connect(self._choose_export_path)
        self.move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected(1))
        layout = QVBoxLayout(self)
        for label, widget in (
            ("复制", self.copy_progress),
            ("校验", self.verify_progress),
        ):
            layout.addWidget(QLabel(label))
            layout.addWidget(widget)
        layout.addWidget(self.status_label)
        layout.addWidget(self.current_file_label)
        layout.addWidget(self.speed_label)
        layout.addWidget(self.eta_label)
        layout.addWidget(self.result_summary)
        layout.addWidget(self.error_details)
        layout.addWidget(self.pause_button)
        layout.addWidget(self.resume_button)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.export_button)
        layout.addWidget(QLabel("任务队列"))
        layout.addWidget(self.queue_list)
        queue_buttons = QHBoxLayout()
        queue_buttons.addWidget(self.move_up_button)
        queue_buttons.addWidget(self.move_down_button)
        layout.addLayout(queue_buttons)

    def export_summary(self, destination: Path) -> None:
        """Export user-facing state only; technical details may contain sensitive paths."""
        lines = (
            "NasMove 任务摘要",
            f"状态：{self.status_label.text()}",
            f"结果：{self.result_summary.text() or '尚无结果'}",
            f"复制进度：{self.copy_progress.value()}%",
            f"校验进度：{self.verify_progress.value()}%",
        )
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _choose_export_path(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "导出脱敏任务摘要",
            "nasmove-report.txt",
            "Text files (*.txt)",
        )
        if filename:
            self.export_summary(Path(filename))

    def set_queue(self, tasks: tuple[object, ...]) -> None:
        self.queue_list.clear()
        for task in tasks:
            task_id = cast(Any, task).id
            name = str(getattr(task, "name", task_id))
            state = getattr(getattr(task, "state", ""), "value", getattr(task, "state", ""))
            item = QListWidgetItem(f"{name}  [{state}]")
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            self.queue_list.addItem(item)
        if self.queue_list.count():
            self.queue_list.setCurrentRow(0)
            self._update_actions(getattr(tasks[0], "state", None))

    def selected_task_id(self) -> object | None:
        item = self.queue_list.currentItem()
        return None if item is None else cast(object, item.data(Qt.ItemDataRole.UserRole))

    def update_selected_state(self, state: object) -> None:
        item = self.queue_list.currentItem()
        if item is None:
            return
        name = item.text().rsplit("  [", 1)[0]
        value = str(getattr(state, "value", state))
        item.setText(f"{name}  [{value}]")

    def _move_selected(self, offset: int) -> None:
        current = self.queue_list.currentRow()
        target = current + offset
        if current < 0 or target < 0 or target >= self.queue_list.count():
            return
        item = self.queue_list.takeItem(current)
        self.queue_list.insertItem(target, item)
        self.queue_list.setCurrentRow(target)
        order = tuple(
            self.queue_list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.queue_list.count())
        )
        self.queue_reordered.emit(order)

    def apply_event(self, event: object) -> None:
        state = getattr(event, "state", None)
        self.status_label.setText(self._state_text(state))
        self._update_actions(state)
        item_id = getattr(event, "item_id", None)
        self.current_file_label.setText("当前文件：—" if item_id is None else f"当前文件：{item_id}")
        error = getattr(event, "error", None)
        if error is not None:
            self.error_details.setPlainText(str(error))

    def apply_snapshot(self, snapshot: ProgressSnapshot, *, state: str | TaskState | None = None) -> None:
        total = max(1, snapshot.total_bytes)
        self.copy_progress.setValue(min(100, int(snapshot.copied_bytes * 100 / max(1, total // 2))))
        self.verify_progress.setValue(min(100, int(snapshot.verified_bytes * 100 / max(1, total // 2))))
        self.speed_label.setText(f"速度：{snapshot.speed_bytes_per_second:.1f} B/s")
        self.eta_label.setText("剩余：—" if snapshot.eta_seconds is None else f"剩余：{snapshot.eta_seconds:.0f} 秒")
        if state is None:
            if snapshot.verified_bytes > 0:
                state = TaskState.VERIFYING
            elif snapshot.copied_bytes > 0:
                state = TaskState.RUNNING
        self.status_label.setText(self._state_text(state))

    @staticmethod
    def _state_text(state: object) -> str:
        values = {
            "verifying": "正在校验",
            "running": "正在传输",
            "paused": "已暂停",
            "waiting_for_network": "等待网络",
            "completed": "已完成",
            "completed_with_warnings": "已完成，但有警告",
            "failed": "失败",
            "canceled": "已取消",
        }
        return values.get(str(getattr(state, "value", state)), "未开始")

    def show_result(self, result: TaskResult | Any) -> None:
        state = getattr(result, "state", None)
        self.status_label.setText(self._state_text(state))
        self._update_actions(state)
        self.update_selected_state(state)
        warnings = tuple(getattr(result, "warnings", ()))
        if str(getattr(state, "value", state)) == "completed_with_warnings" or warnings:
            self.result_summary.setText("迁移完成，但源文件仍保留" if warnings else "迁移完成，但有警告")
        elif bool(getattr(result, "success", False)):
            self.result_summary.setText("迁移完成")
        else:
            self.result_summary.setText("迁移失败")
        error = getattr(result, "error", None)
        self.error_details.setPlainText("" if error is None else str(error))

    def _update_actions(self, state: object) -> None:
        value = str(getattr(state, "value", state))
        self.pause_button.setEnabled(
            value in {"queued", "running", "interrupted", "waiting_for_network"}
        )
        self.resume_button.setEnabled(value == "paused")
        self.cancel_button.setEnabled(
            value in {"draft", "preflight", "queued", "running", "interrupted", "waiting_for_network", "paused"}
        )

    def show_execution_error(self) -> None:
        self.status_label.setText("执行失败，请查看脱敏日志")
        self.result_summary.setText("迁移失败")
        self.error_details.clear()
