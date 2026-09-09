from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from nasmove.core.errors import InvalidRemotePath
from nasmove.ui.connection_page import ConnectionPage
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage


class SetupWorkspace(QWidget):
    edit_connection_requested = Signal()
    edit_sources_requested = Signal()
    edit_target_requested = Signal()
    create_task_requested = Signal()

    def __init__(
        self,
        connection_page: ConnectionPage,
        source_page: SourcePage,
        target_page: TargetPage,
    ) -> None:
        super().__init__()
        self.connection_page = connection_page
        self.source_page = source_page
        self.target_page = target_page
        self._connection_verified = False

        self.connection_summary = QLabel()
        self.source_summary = QLabel()
        self.target_summary = QLabel()
        self.safety_label = QLabel()
        self.safety_label.setWordWrap(True)
        self.create_button = QPushButton("加入队列")
        self.create_button.setProperty("themeRole", "primary")
        self.create_button.setEnabled(False)
        self.editor_stack = QStackedWidget()

        self._build_ui()
        self._connect_signals()
        self.refresh_summary()

    def set_connection_verified(self, verified: bool) -> None:
        self._connection_verified = verified
        self.refresh_summary()

    def refresh_summary(self) -> None:
        self.connection_summary.setText(
            "已验证，可安全浏览 NAS 目录" if self._connection_verified else "尚未测试连接"
        )
        source_count = len(self.source_page.sources)
        self.source_summary.setText(
            "尚未选择来源" if source_count == 0 else f"已选择 {source_count} 个来源"
        )
        target = self._selected_target_text()
        self.target_summary.setText("尚未选择 NAS 目标目录" if target is None else target)
        self.safety_label.setText(self._safety_text())
        self.create_button.setEnabled(
            self._connection_verified and source_count > 0 and target is not None
        )

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        eyebrow = QLabel("可靠迁移工作区")
        eyebrow.setProperty("themeRole", "muted")
        title = QLabel("准备一次安全迁移")
        title.setObjectName("workspaceTitle")
        description = QLabel("完成连接、来源与目标后，即可加入串行任务队列。")
        description.setProperty("themeRole", "muted")
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(description)

        cards = QGridLayout()
        cards.setHorizontalSpacing(12)
        cards.addWidget(
            self._card("01　连接", self.connection_summary, self._show_connection), 0, 0
        )
        cards.addWidget(self._card("02　来源", self.source_summary, self._show_sources), 0, 1)
        cards.addWidget(self._card("03　目标", self.target_summary, self._show_target), 0, 2)
        layout.addLayout(cards)

        self.target_page.add_to_queue_button.hide()
        self.editor_stack.addWidget(self.connection_page)
        self.editor_stack.addWidget(self.source_page)
        self.editor_stack.addWidget(self.target_page)
        layout.addWidget(self.editor_stack, 1)

        safety_card = QFrame()
        safety_card.setProperty("themeRole", "surface")
        safety_layout = QVBoxLayout(safety_card)
        safety_title = QLabel("安全计划")
        safety_title.setObjectName("safetyPlanTitle")
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(self.safety_label)
        layout.addWidget(safety_card)

        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(self.create_button)
        layout.addLayout(actions)

    def _card(self, title: str, summary: QLabel, callback: Callable[[], None]) -> QFrame:
        card = QFrame()
        card.setProperty("themeRole", "surface")
        layout = QVBoxLayout(card)
        button = QPushButton(title)
        button.setObjectName("setupCard-" + title[:2])
        button.setAccessibleName(title)
        button.clicked.connect(callback)
        summary.setWordWrap(True)
        summary.setProperty("themeRole", "muted")
        layout.addWidget(button)
        layout.addWidget(summary)
        return card

    def _connect_signals(self) -> None:
        self.connection_page.report_ready.connect(self._connection_reported)
        self.source_page.sources_changed.connect(lambda _sources: self.refresh_summary())
        self.source_page.move_checkbox.toggled.connect(lambda _checked: self.refresh_summary())
        self.target_page.target_changed.connect(lambda _path: self.refresh_summary())
        self.create_button.clicked.connect(self._request_creation)
        self.create_task_requested.connect(self.target_page.create_task_requested.emit)

    def _connection_reported(self, report: object) -> None:
        self.set_connection_verified(bool(getattr(report, "success", False)))

    def _show_connection(self) -> None:
        self.editor_stack.setCurrentWidget(self.connection_page)
        self.edit_connection_requested.emit()

    def show_connection_editor(self) -> None:
        self._show_connection()

    def _show_sources(self) -> None:
        self.editor_stack.setCurrentWidget(self.source_page)
        self.edit_sources_requested.emit()

    def _show_target(self) -> None:
        self.editor_stack.setCurrentWidget(self.target_page)
        self.edit_target_requested.emit()

    def _request_creation(self) -> None:
        if self.create_button.isEnabled():
            self.create_task_requested.emit()

    def _selected_target_text(self) -> str | None:
        try:
            return self.target_page.target_path().value
        except InvalidRemotePath:
            return None

    def _safety_text(self) -> str:
        if self.source_page.move_checkbox.isChecked():
            return "安全移动：复制 → 完整回读校验 → 原子提交 → 删除源文件"
        return "复制：复制 → 完整回读校验 → 原子提交；不会删除源文件"


__all__ = ["SetupWorkspace"]
