from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from nasmove.core.model import RemotePath
from nasmove.core.states import ConflictPolicy
from nasmove.ui.device_sidebar import DeviceSidebar
from nasmove.ui.directory_models import DirectorySide, DirectorySnapshot
from nasmove.ui.file_pane import FilePane
from nasmove.ui.queue_panel import QueuePanel
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage


class TransferWorkspace(QWidget):
    edit_connection_requested = Signal()
    connect_requested = Signal()
    create_task_requested = Signal()
    task_details_requested = Signal(object)

    def __init__(self, source_page: SourcePage, target_page: TargetPage) -> None:
        super().__init__()
        self.setObjectName("transferWorkspace")
        self.source_page = source_page
        self.target_page = target_page
        self._connection_verified = False
        self._creating = False
        self._remote_location: str | None = None
        self.device_sidebar = DeviceSidebar()
        self.local_pane = FilePane(DirectorySide.LOCAL, "Mac")
        self.remote_pane = FilePane(DirectorySide.REMOTE, "NAS")
        self.queue_panel = QueuePanel()
        self.policy_label = QLabel("冲突策略：")
        self.policy_selector = QComboBox()
        self.policy_selector.setObjectName("conflictPolicy")
        self.policy_selector.addItem("保留两者", ConflictPolicy.KEEP_BOTH)
        self.policy_selector.addItem("覆盖目标", ConflictPolicy.OVERWRITE)
        self.policy_selector.addItem("跳过同名项", ConflictPolicy.SKIP)
        self.policy_selector.addItem("仅源文件较新时覆盖", ConflictPolicy.OVERWRITE_IF_NEWER)
        self.policy_selector.addItem("每次创建时询问", ConflictPolicy.ASK)
        self.policy_selector.setItemData(0, "遇到同名目标时自动重命名保留两者", Qt.ItemDataRole.ToolTipRole)
        self.policy_selector.setItemData(1, "完全覆盖同名目标文件", Qt.ItemDataRole.ToolTipRole)
        self.policy_selector.setItemData(2, "跳过同名同大小文件，保留目标文件", Qt.ItemDataRole.ToolTipRole)
        self.policy_selector.setItemData(3, "仅当本地源文件修改时间更新时才覆盖目标", Qt.ItemDataRole.ToolTipRole)
        self.policy_selector.setItemData(4, "预检时发现同名目标则弹窗询问处理方式", Qt.ItemDataRole.ToolTipRole)
        self.policy_selector.setToolTip("预检和提交阶段都会按所选策略处理同名目标")
        self.selection_label = QLabel("已选 0 项")
        self.selection_label.setProperty("themeRole", "muted")
        self.target_hint_label = QLabel("目标：NAS 未连接")
        self.target_hint_label.setProperty("themeRole", "muted")
        self.upload_button = QPushButton("上传 →")
        self.upload_button.setProperty("themeRole", "primary")
        self.move_button = QPushButton("移动 →")
        self.move_button.setProperty("themeRole", "warning")
        self.pull_button = QPushButton("← 从 NAS 复制")
        self.pull_button.hide()
        self.upload_button.clicked.connect(lambda: self._request_create(move=False))
        self.move_button.clicked.connect(lambda: self._request_create(move=True))
        self.local_pane.selection_changed.connect(self._selection_changed)
        self.remote_pane.selection_changed.connect(self._remote_selection_changed)
        self.remote_pane.connect_requested.connect(self.connect_requested.emit)
        self.remote_pane.set_connection_guidance(True)
        self.device_sidebar.edit_requested.connect(self.edit_connection_requested.emit)
        self.device_sidebar.connect_requested.connect(self.connect_requested.emit)
        self.queue_panel.set_compact(True)
        self.queue_panel.list_widget.itemDoubleClicked.connect(
            lambda item: self.task_details_requested.emit(
                item.data(Qt.ItemDataRole.UserRole)
            )
        )

        browser = QWidget()
        browser.setObjectName("fileBrowserArea")
        browser_layout = QVBoxLayout(browser)
        browser_layout.setContentsMargins(12, 12, 12, 12)
        browser_layout.setSpacing(10)
        toolbar_widget = QWidget()
        toolbar_widget.setObjectName("workspaceToolbar")
        toolbar = QHBoxLayout(toolbar_widget)
        toolbar.setContentsMargins(0, 0, 0, 0)
        direction_label = QLabel("Mac → NAS")
        direction_label.setObjectName("workspaceDirection")
        toolbar.addWidget(direction_label)
        toolbar.addStretch()
        toolbar.addWidget(self.policy_label)
        toolbar.addWidget(self.policy_selector)
        browser_layout.addWidget(toolbar_widget)
        self.browser_splitter = QSplitter()
        self.browser_splitter.setObjectName("fileBrowserSplitter")
        self.browser_splitter.setChildrenCollapsible(False)
        self.browser_splitter.setHandleWidth(6)
        self.browser_splitter.addWidget(self.local_pane)
        self.browser_splitter.addWidget(self.remote_pane)
        self.browser_splitter.setSizes([500, 500])
        browser_layout.addWidget(self.browser_splitter, 1)
        browser_layout.addWidget(self.target_page.creation_status_label)
        actions_widget = QWidget()
        actions_widget.setObjectName("workspaceActions")
        actions = QHBoxLayout(actions_widget)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(self.selection_label)
        actions.addSpacing(12)
        actions.addWidget(self.target_hint_label)
        actions.addStretch()
        actions.addWidget(self.pull_button)
        actions.addWidget(self.move_button)
        actions.addWidget(self.upload_button)
        browser_layout.addWidget(actions_widget)

        self.outer_splitter = QSplitter()
        self.outer_splitter.setObjectName("workbenchSplitter")
        self.outer_splitter.setChildrenCollapsible(False)
        self.outer_splitter.setHandleWidth(6)
        self.outer_splitter.addWidget(self.device_sidebar)
        self.outer_splitter.addWidget(browser)
        self.outer_splitter.addWidget(self.queue_panel)
        self.outer_splitter.setSizes([200, 1044, 56])
        self.outer_splitter.setCollapsible(1, False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.outer_splitter)
        self._refresh_actions()

    def set_connection_verified(self, verified: bool) -> None:
        self._connection_verified = verified
        self.device_sidebar.show_connection_state(online=verified)
        self.remote_pane.set_connection_guidance(not verified)
        self._refresh_actions()

    def set_creating(self, creating: bool) -> None:
        self._creating = creating
        self.local_pane.setEnabled(not creating)
        self.remote_pane.setEnabled(not creating)
        self._refresh_actions()

    def set_remote_location(self, location: str | None) -> None:
        self._remote_location = location or None
        if self._remote_location is not None:
            self.target_page.set_selected_path(RemotePath(self._remote_location))
        else:
            self.target_page.clear_selection()
        self._refresh_actions()

    def clear_remote(self) -> None:
        self.remote_pane.apply_snapshot(
            DirectorySnapshot(DirectorySide.REMOTE, "", (), 0, None)
        )
        self.set_remote_location(None)
        self.remote_pane.set_connection_guidance(not self._connection_verified)

    def apply_directory_snapshot(self, snapshot: DirectorySnapshot) -> None:
        pane = self.local_pane if snapshot.side is DirectorySide.LOCAL else self.remote_pane
        if snapshot.side is DirectorySide.REMOTE:
            self.remote_pane.set_connection_guidance(False)
        pane.apply_snapshot(snapshot)
        if snapshot.side is DirectorySide.REMOTE:
            self.set_remote_location(snapshot.location or None)

    def apply_responsive_width(self, width: int) -> None:
        self.queue_panel.set_compact(width < 980)
        self.device_sidebar.set_compact(width < 860)

    def effective_remote_target(self) -> str | None:
        selected = self.remote_pane.selected_entries()
        if selected and selected[0].is_directory:
            return selected[0].identity
        return self._remote_location or (self.remote_pane.location or None)

    def _remote_selection_changed(self, _entries: object) -> None:
        target = self.effective_remote_target()
        if target is not None:
            self.target_page.set_selected_path(RemotePath(target))
        elif self._remote_location is not None:
            self.target_page.set_selected_path(RemotePath(self._remote_location))
        else:
            self.target_page.clear_selection()
        self._refresh_actions()

    def _selection_changed(self, entries: object) -> None:
        selected = tuple(entries) if isinstance(entries, tuple) else ()
        self.selection_label.setText(f"已选 {len(selected)} 项")
        self._refresh_actions()

    def _request_create(self, *, move: bool) -> None:
        paths = [Path(entry.identity) for entry in self.local_pane.selected_entries()]
        target = self.effective_remote_target()
        if not paths or target is None:
            return
        self.source_page.set_sources(paths)
        self.source_page.move_checkbox.setChecked(move)
        self.source_page.copy_radio.setChecked(not move)
        self.target_page.set_selected_path(target)
        self.create_task_requested.emit()

    def selected_conflict_policy(self) -> ConflictPolicy:
        value = self.policy_selector.currentData()
        return value if isinstance(value, ConflictPolicy) else ConflictPolicy.KEEP_BOTH

    def _refresh_actions(self) -> None:
        target = self.effective_remote_target()
        ready = (
            self._connection_verified
            and not self._creating
            and bool(self.local_pane.selected_entries())
            and target is not None
        )
        self.upload_button.setEnabled(ready)
        self.move_button.setEnabled(ready)
        self.upload_button.setText("正在创建任务…" if self._creating else "上传 →")
        if not self._connection_verified:
            self.target_hint_label.setText("目标：NAS 未连接")
        elif target is not None:
            self.target_hint_label.setText(f"目标：NAS · /{target}")
        else:
            self.target_hint_label.setText("目标：请双击进入或选择具体目标文件夹")


__all__ = ["TransferWorkspace"]
