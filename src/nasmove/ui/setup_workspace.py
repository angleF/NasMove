from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
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
        self._creating = False

        self.connection_summary = QLabel()
        self.source_summary = QLabel()
        self.target_summary = QLabel()
        self.target_summary.setWordWrap(True)
        self.safety_label = QLabel()
        self.safety_label.setWordWrap(True)
        self.create_button = QPushButton("加入队列")
        self.create_button.setProperty("themeRole", "primary")
        self.create_button.setEnabled(False)
        self.readiness_label = QLabel()
        self.readiness_label.setWordWrap(True)

        self._build_ui()
        self._connect_signals()
        self.refresh_summary()

    def set_connection_verified(self, verified: bool) -> None:
        self._connection_verified = verified
        self.refresh_summary()

    @property
    def connection_verified(self) -> bool:
        return self._connection_verified

    def refresh_summary(self) -> None:
        self.reconnect_button.setVisible(not self._connection_verified)
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
            not self._creating and self._connection_verified and source_count > 0 and target is not None
        )
        self.create_button.setText(
            "正在创建任务…" if self._creating else
            "开始移动" if self.source_page.move_checkbox.isChecked() else "开始复制"
        )
        missing = []
        if not self._connection_verified:
            missing.append("连接 NAS")
        if not source_count:
            missing.append("添加文件或文件夹")
        if target is None:
            missing.append("选择目标文件夹")
        self.readiness_label.setText(
            "请先" + "、".join(missing) if missing else "准备就绪，开始后将自动加入队列处理。"
        )

    def set_creating(self, creating: bool) -> None:
        self._creating = creating
        self.source_page.setEnabled(not creating)
        self.target_page.setEnabled(not creating)
        self.choose_target_button.setEnabled(not creating)
        self.refresh_summary()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(14)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        eyebrow = QLabel("可靠迁移工作区")
        eyebrow.setProperty("themeRole", "muted")
        title = QLabel("将文件复制或移动到 NAS")
        title.setObjectName("workspaceTitle")
        description = QLabel("添加本地文件 → 选择目标文件夹 → 开始复制或移动")
        description.setWordWrap(True)
        description.setProperty("themeRole", "muted")
        layout.addWidget(title)
        layout.addWidget(description)
        self.reconnect_button = QPushButton("连接 NAS／更换账号")
        self.reconnect_button.clicked.connect(self.edit_connection_requested.emit)
        layout.addWidget(self.reconnect_button)

        self.target_page.add_to_queue_button.hide()
        self.source_page.setMinimumHeight(250)
        self.source_page.setMaximumHeight(320)
        self.source_page.list_widget.setMinimumHeight(90)
        layout.addWidget(self.source_page, 1)
        target_actions = QHBoxLayout()
        target_actions.addWidget(QLabel("目标文件夹"))
        target_actions.addWidget(self.target_summary, 1)
        self.choose_target_button = QPushButton("选择 NAS 文件夹…")
        self.choose_target_button.setProperty("themeRole", "secondary")
        self.choose_target_button.clicked.connect(self._show_target)
        target_actions.addWidget(self.choose_target_button)
        layout.addLayout(target_actions)

        safety_card = QFrame()
        safety_card.setProperty("themeRole", "surface")
        safety_layout = QVBoxLayout(safety_card)
        safety_title = QLabel("安全计划")
        safety_title.setObjectName("safetyPlanTitle")
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(self.safety_label)
        layout.addWidget(safety_card)

        actions = QHBoxLayout()
        actions.addWidget(self.readiness_label, 1)
        actions.addStretch()
        actions.addWidget(self.create_button)
        root.addLayout(actions)

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
        self.edit_connection_requested.emit()

    def show_connection_editor(self) -> None:
        self._show_connection()

    def _show_sources(self) -> None:
        self.edit_sources_requested.emit()

    def _show_target(self) -> None:
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
            return "安全移动：复制 → 完整回读校验 → 原子提交 → 移入废纸篓"
        return "复制：复制 → 完整回读校验 → 原子提交；不会处理源文件"


__all__ = ["SetupWorkspace"]
