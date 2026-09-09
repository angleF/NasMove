from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Any, cast

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from nasmove.core.states import TaskState
from nasmove.smb.error_mapping import redacted_error_code
from nasmove.transfer.progress import ProgressSnapshot
from nasmove.transfer.transfer_engine import TaskResult
from nasmove.ui.task_presentation import ERROR_TEXT, STATE_TEXT, duration_text, safe_code, size_text


class TaskPage(QWidget):
    pause_requested = Signal()
    resume_requested = Signal()
    cancel_requested = Signal()
    queue_reordered = Signal(object)
    selection_changed = Signal(object)
    connection_requested = Signal()
    files_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.copy_progress = QProgressBar()
        self.verify_progress = QProgressBar()
        self.status_label = QLabel("尚未创建任务")
        self.status_label.setObjectName("taskStatus")
        self.task_name_label = QLabel("开始一次可靠迁移")
        self.task_name_label.setObjectName("taskName")
        self.location_label = QLabel("点击右上角“新建任务”，选择本地文件与 NAS 目标目录。")
        self.location_label.setWordWrap(True)
        self.safety_label = QLabel("创建任务后，这里会显示文件处理结果。")
        self.safety_label.setWordWrap(True)
        self.activity_label = QLabel()
        self._last_update = monotonic()
        self._state: object = None
        self._action = "copy"
        self._total_files = 0
        self._error_code: str | None = None
        self._retry_deadline: float | None = None
        self._retry_attempt: int | None = None
        self.current_file_label = QLabel("当前文件：—")
        self.current_file_label.setWordWrap(True)
        self.speed_label = QLabel("速度：—")
        self.eta_label = QLabel("剩余：—")
        self.result_summary = QLabel()
        self.result_summary.setWordWrap(True)
        self.error_details = QTextEdit()
        self.error_details.setReadOnly(True)
        self.error_details.setMaximumHeight(140)
        self.error_details.hide()
        self.details_button = QPushButton("▸ 错误详情与处理记录")
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(self.error_details.setVisible)
        self.connection_button = QPushButton("检查 NAS 连接")
        self.connection_button.clicked.connect(self.connection_requested.emit)
        self.files_button = QPushButton("查看文件结果")
        self.files_button.clicked.connect(self.files_requested.emit)
        self.target_button = QPushButton("在 Finder 中查看目标目录")
        self._target_url = QUrl()
        self.target_button.clicked.connect(self._open_target)
        self.pause_button = QPushButton("暂停")
        self.resume_button = QPushButton("继续")
        self.cancel_button = QPushButton("取消")
        self.export_button = QPushButton("导出脱敏摘要")
        self.queue_list = QListWidget()
        self.queue_list.currentItemChanged.connect(
            lambda *_: self.selection_changed.emit(self.selected_task_id())
        )
        self.move_up_button = QPushButton("上移")
        self.move_down_button = QPushButton("下移")
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.resume_button.clicked.connect(self.resume_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.export_button.clicked.connect(self._choose_export_path)
        self.move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected(1))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter()
        layout.addWidget(splitter)
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.addWidget(QLabel("全部任务 · 串行执行"))
        side.addWidget(self.queue_list)
        queue_buttons = QHBoxLayout()
        queue_buttons.addWidget(self.move_up_button)
        queue_buttons.addWidget(self.move_down_button)
        side.addLayout(queue_buttons)
        splitter.addWidget(sidebar)
        content = QWidget()
        content.setObjectName("taskDetail")
        detail = QVBoxLayout(content)
        detail.setContentsMargins(24, 20, 24, 20)
        detail.setSpacing(14)
        detail.addWidget(self.task_name_label)
        detail.addWidget(self.location_label)
        detail.addWidget(self.status_label)
        detail.addWidget(self.result_summary)
        detail.addWidget(self.activity_label)
        self.progress_panel = QWidget()
        progress_layout = QVBoxLayout(self.progress_panel)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        for label, widget in (
            ("复制", self.copy_progress),
            ("校验", self.verify_progress),
        ):
            widget.setValue(0)
            widget.setTextVisible(True)
            progress_layout.addWidget(QLabel(label))
            progress_layout.addWidget(widget)
        progress_layout.addWidget(self.current_file_label)
        metrics = QHBoxLayout()
        metrics.addWidget(self.speed_label)
        metrics.addWidget(self.eta_label)
        progress_layout.addLayout(metrics)
        detail.addWidget(self.progress_panel)
        detail.addWidget(self.safety_label)
        actions = QHBoxLayout()
        for button in (self.pause_button, self.resume_button, self.cancel_button, self.connection_button):
            actions.addWidget(button)
        actions.addStretch()
        detail.addLayout(actions)
        detail.addWidget(self.files_button, alignment=Qt.AlignmentFlag.AlignLeft)
        detail.addWidget(self.target_button, alignment=Qt.AlignmentFlag.AlignLeft)
        detail.addWidget(self.details_button)
        detail.addWidget(self.error_details)
        detail.addStretch()
        detail.addWidget(self.export_button, alignment=Qt.AlignmentFlag.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        splitter.addWidget(scroll)
        splitter.setSizes([260, 760])
        splitter.setStretchFactor(1, 1)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_activity)
        self._timer.start(1000)
        self._update_actions(None)
        self.progress_panel.hide()
        self.details_button.hide()
        self.export_button.hide()
        self.files_button.hide()
        self.target_button.hide()

    def show_task(self, task: object) -> None:
        self._action = str(getattr(getattr(task, "action", "copy"), "value", "copy"))
        self._total_files = int(getattr(task, "total_files", 0))
        self.task_name_label.setText(str(getattr(task, "name", "迁移任务")))
        connection = getattr(task, "connection", None)
        self._target_url = QUrl()
        self._target_url.setScheme("smb")
        self._target_url.setHost(str(getattr(connection, "host", "")))
        self._target_url.setPort(int(getattr(connection, "port", 445)))
        self._target_url.setPath(f"/{getattr(connection, 'share', '')}/{getattr(task, 'target_root', '')}")
        self.target_button.show()
        mode = "安全移动" if self._action == "move" else "复制"
        self.location_label.setText(
            f"{mode} · {self._total_files} 个文件 · "
            f"目标：{getattr(connection, 'share', '')}/{getattr(task, 'target_root', '')}"
        )
        self.error_details.clear()
        self._error_code = None
        self.details_button.setChecked(False)
        self.details_button.hide()
        self.result_summary.clear()
        self.safety_label.setText("文件处理结果将在完成后确认；复制与校验完成前不会删除源文件。")
        self.current_file_label.setText("当前文件：—")
        self.speed_label.setText("速度：—")
        self.eta_label.setText("剩余：正在估算")
        self.progress_panel.show()
        self.export_button.show()
        self.files_button.show()
        total = int(getattr(task, "total_bytes", 0))
        self.apply_snapshot(ProgressSnapshot(total * 2, 0, int(getattr(task, "copied_bytes", 0)),
            int(getattr(task, "verified_bytes", 0)), 0, None), state=getattr(task, "state", None))
        self._update_actions(getattr(task, "state", None))

    def _update_activity(self) -> None:
        value = str(getattr(self._state, "value", self._state))
        if value == "waiting_for_network" and self._retry_deadline is not None:
            remaining = max(0, int(self._retry_deadline - monotonic()))
            self.result_summary.setText(
                f"连接中断，正在自动重连。第 {self._retry_attempt} 次重试，{remaining} 秒后再次尝试。"
                if remaining else "正在重新连接 NAS，等待连接结果…"
            )
        if value in {"running", "verifying", "committing", "deleting_source", "waiting_for_network"}:
            elapsed = int(monotonic() - self._last_update)
            self.activity_label.setText(
                f"最近更新：{elapsed} 秒前" if elapsed < 30 else
                f"已 {elapsed} 秒未收到新进度，任务尚未报告完成。可检查连接或等待下一次更新。"
            )
        else:
            self.activity_label.clear()

    def _open_target(self) -> None:
        if not QDesktopServices.openUrl(self._target_url):
            self.result_summary.setText("无法打开 Finder。请通过“查看文件结果”核对目标路径。")

    def export_summary(self, destination: Path) -> None:
        """Export user-facing state only; technical details may contain sensitive paths."""
        lines = (
            "NasMove 任务摘要",
            f"状态：{self.status_label.text()}",
            f"结果：{self.result_summary.text() or '尚无结果'}",
            f"复制进度：{self.copy_progress.value()}%",
            f"校验进度：{self.verify_progress.value()}%",
            f"错误代码：{self._error_code or '无'}",
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
        selected = self.selected_task_id()
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        for task in tasks:
            task_id = cast(Any, task).id
            name = str(getattr(task, "name", task_id))
            state = getattr(getattr(task, "state", ""), "value", getattr(task, "state", ""))
            item = QListWidgetItem(f"{name}  [{self._state_text(state)}]")
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, state)
            self.queue_list.addItem(item)
        if self.queue_list.count():
            row = next((i for i, task in enumerate(tasks) if getattr(task, "id", None) == selected), 0)
            self.queue_list.setCurrentRow(row)
        self.queue_list.blockSignals(False)
        self.selection_changed.emit(self.selected_task_id())

    def selected_task_id(self) -> object | None:
        item = self.queue_list.currentItem()
        return None if item is None else cast(object, item.data(Qt.ItemDataRole.UserRole))

    def update_selected_state(self, state: object) -> None:
        item = self.queue_list.currentItem()
        if item is None:
            return
        name = item.text().rsplit("  [", 1)[0]
        value = self._state_text(state)
        item.setText(f"{name}  [{value}]")

    def _move_selected(self, offset: int) -> None:
        current = self.queue_list.currentRow()
        target = current + offset
        if current < 0 or target < 0 or target >= self.queue_list.count():
            return
        if any(self.queue_list.item(row).data(Qt.ItemDataRole.UserRole + 1) != "queued" for row in (current, target)):
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
        self._last_update = monotonic()
        self.progress_panel.show()
        self.status_label.setText(self._state_text(state))
        self._update_actions(state)
        if getattr(state, "value", state) in {"queued", "running", "verifying", "committing", "deleting_source"}:
            self.result_summary.clear()
            self._retry_deadline = None
            self.error_details.clear()
            self.details_button.setChecked(False)
            self.details_button.hide()
        self.update_selected_state(state)
        item_id = getattr(event, "item_id", None)
        if item_id is not None:
            self.current_file_label.setText("当前文件：正在处理，请展开任务文件信息")
        error = getattr(event, "error", None)
        if error is not None:
            self._show_reason(redacted_error_code(error))
        attempt = getattr(event, "retry_attempt", None)
        delay = getattr(event, "retry_delay", None)
        if attempt is not None and delay is not None:
            self._retry_deadline = monotonic() + delay
            self._retry_attempt = attempt
            self.result_summary.setText(f"连接中断，正在自动重连。第 {attempt} 次重试，约 {delay:.0f} 秒后再次尝试。")

    def apply_snapshot(self, snapshot: ProgressSnapshot, *, state: str | TaskState | None = None) -> None:
        total = max(1, snapshot.total_bytes)
        self._last_update = monotonic()
        self.copy_progress.setValue(min(100, int(snapshot.copied_bytes * 100 / max(1, total // 2))))
        self.verify_progress.setValue(min(100, int(snapshot.verified_bytes * 100 / max(1, total // 2))))
        self.copy_progress.setFormat(f"%p% · {size_text(snapshot.copied_bytes)} / {size_text(snapshot.total_bytes / 2)}")
        self.verify_progress.setFormat(f"%p% · {size_text(snapshot.verified_bytes)} / {size_text(snapshot.total_bytes / 2)}")
        self.speed_label.setText(f"速度：{size_text(snapshot.speed_bytes_per_second)}/s")
        self.eta_label.setText(f"剩余：{duration_text(snapshot.eta_seconds)}")
        if state is None:
            if snapshot.verified_bytes > 0:
                state = TaskState.VERIFYING
            elif snapshot.copied_bytes > 0:
                state = TaskState.RUNNING
        self.status_label.setText(self._state_text(state))
        self._update_actions(state)

    @staticmethod
    def _state_text(state: object) -> str:
        return STATE_TEXT.get(str(getattr(state, "value", state)), "尚未创建任务")

    def show_result(self, result: TaskResult | Any) -> None:
        self.error_details.clear()
        self._error_code = None
        self.details_button.setChecked(False)
        self.details_button.hide()
        state = getattr(result, "state", None)
        self.status_label.setText(self._state_text(state))
        self._update_actions(state)
        self.update_selected_state(state)
        warnings = tuple(getattr(result, "warnings", ()))
        value = str(getattr(state, "value", state))
        if value != "waiting_for_network":
            self._retry_deadline = None
        if value == "completed_with_warnings" or warnings:
            self.result_summary.setText("迁移完成，但源文件仍保留" if warnings else "迁移完成，但有警告")
            self.safety_label.setText("部分文件需要处理，请核对文件结果；不要重复删除源文件。")
        elif bool(getattr(result, "success", False)):
            self.result_summary.setText("迁移完成")
            self.safety_label.setText("目标文件已校验并提交。" + (
                "移动流程已完成。" if self._action == "move" else "源文件保留在本机。"
            ))
            for bar in (self.copy_progress, self.verify_progress):
                bar.setValue(100)
                bar.setFormat("100% · 已完成")
        elif value in {"paused", "canceled", "waiting_for_network"}:
            self.result_summary.setText({"paused": "任务已暂停，继续后核对断点再传输。",
                "canceled": "任务已取消。已提交的目标文件保留，请核对文件结果。",
                "waiting_for_network": "连接暂时不可用，正在等待网络恢复。"}[value])
        else:
            self.result_summary.setText("任务未完成，请查看原因及处理建议。")
            self.safety_label.setText("可能已有部分目标文件，源文件和目标文件状态需要核对。")
        error = getattr(result, "error", None)
        if error is not None:
            self._show_reason(redacted_error_code(error))
        if value in {"failed", "completed", "completed_with_warnings", "canceled", "paused"}:
            self.speed_label.setText("速度：—")
            self.eta_label.setText("剩余：—")

    def _update_actions(self, state: object) -> None:
        self._state = state
        value = str(getattr(state, "value", state))
        self.pause_button.setEnabled(
            value in {"queued", "running", "interrupted", "waiting_for_network"}
        )
        self.resume_button.setEnabled(value == "paused")
        self.cancel_button.setEnabled(
            value in {"draft", "preflight", "queued", "running", "interrupted", "waiting_for_network", "paused"}
        )
        for button in (self.pause_button, self.resume_button, self.cancel_button):
            button.setVisible(button.isEnabled())
        self.connection_button.setVisible(value in {"failed", "interrupted", "paused", "waiting_for_network"})
        color = "#16835d" if value == "completed" else "#b54708" if value in {
            "failed", "paused", "interrupted", "waiting_for_network", "completed_with_warnings"
        } else "#175cd3"
        self.status_label.setStyleSheet(f"font-size: 26px; font-weight: 700; color: {color}; padding: 12px 0;")
        self._update_activity()

    def _show_reason(self, code: str) -> None:
        code = safe_code(code)
        self._error_code = code
        self.result_summary.setText(ERROR_TEXT[code])
        self.error_details.setPlainText(f"错误代码：{code}\n{ERROR_TEXT[code]}")
        self.details_button.show()
        self.export_button.show()

    def show_execution_error(self, code: str = "unexpected_error") -> None:
        self._update_actions("failed")
        self.status_label.setText("执行已停止 · 需要处理")
        self._show_reason(code)
        self.safety_label.setText("任务已停止。可能有部分文件已传输，请核对文件结果后再处理。")
        self.speed_label.setText("速度：—")
        self.eta_label.setText("剩余：—")
