from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, cast

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from nasmove.core.model import TaskId
from nasmove.core.states import TaskState, TransferAction
from nasmove.transfer.progress import ProgressSnapshot
from nasmove.ui.task_presentation import STATE_TEXT


def _percent(part: int, total: int) -> int:
    """Round a byte ratio to the nearest percent, capped at 100.

    Truncating here made a transfer that was one data block short of the end
    read as 99% forever.
    """
    return max(0, min(100, int(part * 100 / total + 0.5)))


@dataclass(frozen=True, slots=True)
class QueueRowState:
    task_id: TaskId
    name: str
    action: TransferAction
    state: TaskState
    total_files: int
    copied_percent: int = 0
    verified_percent: int = 0


class QueuePanel(QWidget):
    task_selected = Signal(object)
    pause_requested = Signal(object)
    resume_requested = Signal(object)
    cancel_requested = Signal(object)
    move_to_top_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("queuePanel")
        self.is_compact = False
        self._rows: dict[object, QueueRowState] = {}
        self._terminal: set[object] = set()
        self.heading = QLabel("传输队列")
        self.heading.setObjectName("queueTitle")
        self.count_label = QLabel("0 个任务")
        self.count_label.setProperty("themeRole", "muted")
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("queueList")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("尚无活动任务")
        self.pause_button = QPushButton("暂停")
        self.pause_button.setObjectName("queuePauseButton")
        self.resume_button = QPushButton("继续")
        self.resume_button.setObjectName("queueResumeButton")
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("queueCancelButton")
        self.top_button = QPushButton("置顶")
        self.top_button.setObjectName("queueTopButton")
        self.fold_button = QPushButton("›")
        self.fold_button.setAccessibleName("收起传输队列")
        self.fold_button.clicked.connect(lambda: self.set_compact(not self.is_compact))
        self.list_widget.currentItemChanged.connect(self._selection_changed)
        self.pause_button.clicked.connect(lambda: self._emit_for_selected(self.pause_requested))
        self.resume_button.clicked.connect(lambda: self._emit_for_selected(self.resume_requested))
        self.cancel_button.clicked.connect(lambda: self._emit_for_selected(self.cancel_requested))
        self.top_button.clicked.connect(lambda: self._emit_for_selected(self.move_to_top_requested))

        top = QHBoxLayout()
        top.addWidget(self.heading)
        top.addWidget(self.count_label)
        top.addStretch()
        top.addWidget(self.fold_button)
        actions = QHBoxLayout()
        for button in (self.top_button, self.pause_button, self.resume_button, self.cancel_button):
            actions.addWidget(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addLayout(top)
        layout.addWidget(self.list_widget, 1)
        layout.addLayout(actions)
        layout.addWidget(self.progress)
        self._update_actions()

    def set_tasks(self, tasks: tuple[object, ...]) -> None:
        selected = self.selected_task_id()
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self.list_widget.blockSignals(False)
        self._rows = {}
        self._terminal = set()
        for task in tasks:
            state = cast(TaskState, getattr(task, "state", TaskState.DRAFT))
            row = QueueRowState(
                task_id=cast(TaskId, cast(Any, task).id),
                name=str(getattr(task, "name", "迁移任务")),
                action=cast(TransferAction, getattr(task, "action", TransferAction.COPY)),
                state=state,
                total_files=int(getattr(task, "total_files", 0)),
            )
            self._rows[row.task_id] = row
            if state in self._terminal_states():
                self._terminal.add(row.task_id)
            item = QListWidgetItem(self._text(row))
            item.setData(Qt.ItemDataRole.UserRole, row.task_id)
            self.list_widget.addItem(item)
        self.count_label.setText(f"{len(self._rows)} 个任务")
        if self.is_compact:
            self.fold_button.setText(f"队\n{len(self._rows)}")
        if selected in self._rows:
            self.select_task(selected)
            self._update_selected_progress(selected)
        elif self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        else:
            self._update_selected_progress(None)
        self._update_actions()

    def upsert_task(self, task: object) -> None:
        task_id = getattr(task, "id", None)
        state = cast(TaskState, getattr(task, "state", TaskState.DRAFT))
        row = QueueRowState(
            task_id=cast(TaskId, task_id),
            name=str(getattr(task, "name", "迁移任务")),
            action=cast(TransferAction, getattr(task, "action", TransferAction.COPY)),
            state=state,
            total_files=int(getattr(task, "total_files", 0)),
        )
        item = self._item(task_id)
        self._rows[task_id] = row
        if item is None:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            self.list_widget.addItem(item)
        item.setText(self._text(row))
        self.count_label.setText(f"{len(self._rows)} 个任务")
        if self.is_compact:
            self.fold_button.setText(f"队\n{len(self._rows)}")
        self.select_task(task_id)
        self._update_selected_progress(task_id)
        self._update_actions()

    def apply_event(self, event: object) -> None:
        task_id = getattr(event, "task_id", None)
        state = getattr(event, "state", None)
        row = self._rows.get(task_id)
        if row is None or not isinstance(state, TaskState):
            return
        self._rows[task_id] = replace(row, state=state)
        if state not in self._terminal_states():
            self._terminal.discard(task_id)
        self._refresh_item(task_id)
        if self.selected_task_id() == task_id:
            self._update_selected_progress(task_id)

    def apply_progress(self, task_id: object, snapshot: ProgressSnapshot) -> None:
        row = self._rows.get(task_id)
        if row is None or task_id in self._terminal:
            return
        source_total = max(1, snapshot.total_bytes // 2)
        updated = replace(
            row,
            copied_percent=_percent(snapshot.copied_bytes, source_total),
            verified_percent=_percent(snapshot.verified_bytes, source_total),
        )
        self._rows[task_id] = updated
        self._refresh_item(task_id)
        if self.selected_task_id() == task_id:
            value = _percent(snapshot.completed_bytes, max(1, snapshot.total_bytes))
            self.progress.setValue(value)
            self.progress.setFormat(
                f"复制 {updated.copied_percent}% · 校验 {updated.verified_percent}%"
            )

    def apply_result(self, result: object, *, task_id: object | None = None) -> None:
        task_id = task_id or getattr(result, "task_id", None)
        state = getattr(result, "state", None)
        row = self._rows.get(task_id)
        if row is None or not isinstance(state, TaskState):
            return
        finished_successfully = state in self._successful_terminal_states()
        copied = 100 if finished_successfully else row.copied_percent
        verified = 100 if finished_successfully else row.verified_percent
        self._rows[task_id] = replace(
            row, state=state, copied_percent=copied, verified_percent=verified
        )
        if state in self._terminal_states():
            self._terminal.add(task_id)
        self._refresh_item(task_id)
        if self.selected_task_id() == task_id:
            self._update_selected_progress(task_id)

    def row_count(self) -> int:
        return self.list_widget.count()

    def row_text(self, task_id: object) -> str:
        item = self._item(task_id)
        return "" if item is None else item.text()

    def state_text(self, task_id: object) -> str:
        row = self._rows.get(task_id)
        if row is None:
            return ""
        return STATE_TEXT.get(row.state.value, row.state.value)

    def progress_value(self, task_id: object) -> int:
        row = self._rows.get(task_id)
        if row is None:
            return 0
        return min(100, (row.copied_percent + row.verified_percent) // 2)

    def selected_task_id(self) -> object | None:
        item = self.list_widget.currentItem()
        return None if item is None else cast(object, item.data(Qt.ItemDataRole.UserRole))

    def select_task(self, task_id: object) -> None:
        item = self._item(task_id)
        if item is not None:
            self.list_widget.setCurrentItem(item)

    def set_compact(self, compact: bool) -> None:
        self.is_compact = compact
        self.setFixedWidth(56 if compact else 284)
        layout = self.layout()
        if layout is not None:
            if compact:
                layout.setContentsMargins(4, 12, 4, 12)
            else:
                layout.setContentsMargins(12, 12, 12, 12)
        if compact:
            self.fold_button.setFixedWidth(40)
        else:
            self.fold_button.setMinimumWidth(0)
            self.fold_button.setMaximumWidth(16_777_215)
        for widget in (
            self.heading,
            self.count_label,
            self.list_widget,
            self.progress,
            self.pause_button,
            self.resume_button,
            self.cancel_button,
            self.top_button,
        ):
            widget.setVisible(not compact)
        self.fold_button.setText(f"队\n{len(self._rows)}" if compact else "›")
        self.fold_button.setAccessibleName("展开传输队列" if compact else "收起传输队列")

    def _selection_changed(self, current: QListWidgetItem | None, _previous: object) -> None:
        self._update_actions()
        task_id = None if current is None else current.data(Qt.ItemDataRole.UserRole)
        self._update_selected_progress(task_id)
        if current is not None:
            self.task_selected.emit(task_id)

    def _update_selected_progress(self, task_id: object | None) -> None:
        if task_id is None or task_id not in self._rows:
            self.progress.setValue(0)
            self.progress.setFormat("尚无活动任务")
            return
        row = self._rows[task_id]
        if row.state in self._successful_terminal_states():
            self.progress.setValue(100)
            self.progress.setFormat("复制 100% · 校验 100%")
        elif row.state is TaskState.FAILED:
            self.progress.setValue(self.progress_value(task_id))
            self.progress.setFormat(
                f"失败 · 复制 {row.copied_percent}% · 校验 {row.verified_percent}%"
            )
        elif row.state is TaskState.CANCELED:
            self.progress.setValue(self.progress_value(task_id))
            self.progress.setFormat(
                f"已取消 · 复制 {row.copied_percent}% · 校验 {row.verified_percent}%"
            )
        elif row.state is TaskState.PAUSED:
            self.progress.setValue(self.progress_value(task_id))
            self.progress.setFormat(
                f"已暂停 · 复制 {row.copied_percent}% · 校验 {row.verified_percent}%"
            )
        elif row.copied_percent == 0 and row.verified_percent == 0:
            self.progress.setValue(0)
            self.progress.setFormat("等待开始")
        else:
            self.progress.setValue(self.progress_value(task_id))
            self.progress.setFormat(
                f"复制 {row.copied_percent}% · 校验 {row.verified_percent}%"
            )

    def _emit_for_selected(self, signal: Any) -> None:
        task_id = self.selected_task_id()
        if task_id is not None:
            signal.emit(task_id)

    def _update_actions(self) -> None:
        task_id = self.selected_task_id()
        row = self._rows.get(task_id) if task_id is not None else None
        state = None if row is None else row.state
        self.top_button.setEnabled(state is TaskState.QUEUED)
        self.pause_button.setEnabled(
            state in {TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING_FOR_NETWORK}
        )
        self.resume_button.setEnabled(state is TaskState.PAUSED)
        self.cancel_button.setEnabled(
            state
            in {
                TaskState.DRAFT,
                TaskState.PREFLIGHT,
                TaskState.QUEUED,
                TaskState.RUNNING,
                TaskState.WAITING_FOR_NETWORK,
                TaskState.PAUSED,
            }
        )

    def _refresh_item(self, task_id: object) -> None:
        item = self._item(task_id)
        if item is not None:
            item.setText(self._text(self._rows[task_id]))
        self._update_actions()

    def _item(self, task_id: object) -> QListWidgetItem | None:
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == task_id:
                return item
        return None

    @staticmethod
    def _text(row: QueueRowState) -> str:
        action = "移动" if row.action is TransferAction.MOVE else "上传"
        state = STATE_TEXT.get(row.state.value, row.state.value)
        return f"{action} · {row.name}\n{row.total_files} 个文件 · {state}"

    @staticmethod
    def _successful_terminal_states() -> frozenset[TaskState]:
        """Terminal states that represent a finished, successful transfer.

        Kept a strict subset of ``_terminal_states()`` so "done means 100%"
        covers ``COMPLETED_WITH_WARNINGS`` without ever claiming FAILED or
        CANCELED finished successfully.
        """
        return frozenset({TaskState.COMPLETED, TaskState.COMPLETED_WITH_WARNINGS})

    @staticmethod
    def _terminal_states() -> frozenset[TaskState]:
        return frozenset(
            {
                TaskState.COMPLETED,
                TaskState.COMPLETED_WITH_WARNINGS,
                TaskState.FAILED,
                TaskState.CANCELED,
            }
        )


__all__ = ["QueuePanel", "QueueRowState"]
