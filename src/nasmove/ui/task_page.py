from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QProgressBar, QPushButton, QTextEdit, QVBoxLayout, QWidget

from nasmove.core.states import TaskState
from nasmove.transfer.progress import ProgressSnapshot
from nasmove.transfer.transfer_engine import TaskResult


class TaskPage(QWidget):
    pause_requested = Signal()
    resume_requested = Signal()
    cancel_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.copy_progress = QProgressBar()
        self.verify_progress = QProgressBar()
        self.status_label = QLabel("未开始")
        self.speed_label = QLabel("速度：—")
        self.eta_label = QLabel("剩余：—")
        self.result_summary = QLabel()
        self.error_details = QTextEdit()
        self.error_details.setReadOnly(True)
        self.pause_button = QPushButton("暂停")
        self.resume_button = QPushButton("继续")
        self.cancel_button = QPushButton("取消")
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.resume_button.clicked.connect(self.resume_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        layout = QVBoxLayout(self)
        for label, widget in (
            ("复制", self.copy_progress),
            ("校验", self.verify_progress),
        ):
            layout.addWidget(QLabel(label))
            layout.addWidget(widget)
        layout.addWidget(self.status_label)
        layout.addWidget(self.speed_label)
        layout.addWidget(self.eta_label)
        layout.addWidget(self.result_summary)
        layout.addWidget(self.error_details)
        layout.addWidget(self.pause_button)
        layout.addWidget(self.resume_button)
        layout.addWidget(self.cancel_button)

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
        }
        return values.get(str(getattr(state, "value", state)), "未开始")

    def show_result(self, result: TaskResult | Any) -> None:
        state = getattr(result, "state", None)
        warnings = tuple(getattr(result, "warnings", ()))
        if str(getattr(state, "value", state)) == "completed_with_warnings" or warnings:
            self.result_summary.setText("迁移完成，但源文件仍保留" if warnings else "迁移完成，但有警告")
        elif bool(getattr(result, "success", False)):
            self.result_summary.setText("迁移完成")
        else:
            self.result_summary.setText("迁移失败")
        error = getattr(result, "error", None)
        self.error_details.setPlainText("" if error is None else str(error))
