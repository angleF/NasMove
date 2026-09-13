from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from nasmove.core.model import ConnectionConfig, ConnectionProfileId


def _size_text(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class DeviceSidebar(QWidget):
    edit_requested = Signal()
    connect_requested = Signal()
    add_requested = Signal()
    profile_selected = Signal(object)
    remove_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("deviceSidebar")
        self.is_compact = False
        self._profiles: dict[ConnectionProfileId, ConnectionConfig] = {}
        self._profile_actions_enabled = True
        self._display_name = "尚未配置 NAS"
        self.heading_label = QLabel("配置")
        self.name_label = QLabel("尚未配置 NAS")
        self.name_label.setObjectName("deviceName")
        self.protocol_label = QLabel("SMB")
        self.protocol_label.setProperty("themeRole", "muted")
        self.status_label = QLabel("未连接")
        self.capacity_label = QLabel("容量尚未查询")
        self.capacity_label.setProperty("themeRole", "muted")
        self.profile_list = QListWidget()
        self.profile_list.setAccessibleName("NAS 配置列表")
        self.profile_list.currentItemChanged.connect(self._selection_changed)
        self.add_button = QPushButton("＋ 新增")
        self.add_button.setObjectName("deviceAddButton")
        self.add_button.setAccessibleName("新增 NAS 配置")
        self.edit_button = QPushButton("编辑配置")
        self.edit_button.setObjectName("deviceEditButton")
        self.edit_button.setAccessibleName("编辑当前 NAS 配置")
        self.remove_button = QPushButton("移除")
        self.remove_button.setObjectName("deviceRemoveButton")
        self.remove_button.setAccessibleName("移除当前 NAS 配置")
        self.connect_button = QPushButton("连接 NAS")
        self.connect_button.setObjectName("deviceConnectButton")
        self.add_button.clicked.connect(lambda *_: self.add_requested.emit())
        self.edit_button.clicked.connect(lambda *_: self.edit_requested.emit())
        self.remove_button.clicked.connect(lambda *_: self._request_remove())
        self.connect_button.clicked.connect(lambda *_: self.connect_requested.emit())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 16, 14, 16)
        layout.addWidget(self.heading_label)
        layout.addWidget(self.name_label)
        layout.addWidget(self.protocol_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.profile_list, 1)
        profile_actions = QHBoxLayout()
        profile_actions.addWidget(self.add_button)
        profile_actions.addWidget(self.remove_button)
        layout.addLayout(profile_actions)
        layout.addWidget(self.edit_button)
        layout.addWidget(self.connect_button)
        layout.addWidget(self.capacity_label)
        self._refresh_action_buttons()

    @property
    def protocol_selector(self) -> None:
        return None

    def profile_count(self) -> int:
        return len(self._profiles)

    def current_profile_id(self) -> ConnectionProfileId | None:
        item = self.profile_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return ConnectionProfileId(str(value))

    def set_profiles(
        self,
        profiles: tuple[ConnectionConfig, ...],
        *,
        selected_profile_id: ConnectionProfileId | None = None,
        select_first: bool = True,
    ) -> None:
        self._profiles = {config.profile_id: config for config in profiles}
        previous = self.profile_list.blockSignals(True)
        try:
            self.profile_list.clear()
            selected_row = -1
            for row, config in enumerate(profiles):
                item = QListWidgetItem(config.display_name)
                item.setData(Qt.ItemDataRole.UserRole, str(config.profile_id))
                item.setToolTip(f"SMB · {config.host}/{config.share}")
                self.profile_list.addItem(item)
                if config.profile_id == selected_profile_id:
                    selected_row = row
            if selected_row < 0 and profiles and select_first:
                selected_row = 0
            self.profile_list.setCurrentRow(selected_row)
        finally:
            self.profile_list.blockSignals(previous)
        current_id = self.current_profile_id()
        if current_id is None:
            self._show_no_profile()
        else:
            self._show_profile_summary(self._profiles[current_id])
        self._refresh_action_buttons()

    def show_profile(self, config: ConnectionConfig) -> None:
        profiles = tuple(
            candidate
            for candidate in self._profiles.values()
            if candidate.profile_id != config.profile_id
        ) + (config,)
        self.set_profiles(profiles, selected_profile_id=config.profile_id)

    def _show_profile_summary(self, config: ConnectionConfig) -> None:
        self._display_name = config.display_name
        self.name_label.setText(config.display_name)
        self.protocol_label.setText(f"SMB · {config.host}")

    def clear_profile(self) -> None:
        self.set_profiles(())

    def _show_no_profile(self) -> None:
        self._display_name = "尚未配置 NAS"
        self.name_label.setText("尚未配置 NAS")
        self.protocol_label.setText("SMB")
        self.show_connection_state(online=False)
        self.show_capacity(None)

    def set_profile_actions_enabled(self, enabled: bool) -> None:
        self._profile_actions_enabled = enabled
        self._refresh_action_buttons()

    def _refresh_action_buttons(self) -> None:
        has_selection = self.current_profile_id() is not None
        self.profile_list.setEnabled(self._profile_actions_enabled)
        self.add_button.setEnabled(self._profile_actions_enabled)
        self.edit_button.setEnabled(self._profile_actions_enabled and has_selection)
        self.remove_button.setEnabled(self._profile_actions_enabled and has_selection)

    def _selection_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            self._show_no_profile()
            self._refresh_action_buttons()
            return
        profile_id = ConnectionProfileId(
            str(current.data(Qt.ItemDataRole.UserRole))
        )
        config = self._profiles.get(profile_id)
        if config is None:
            return
        self._show_profile_summary(config)
        self._refresh_action_buttons()
        self.profile_selected.emit(profile_id)

    def _request_remove(self) -> None:
        profile_id = self.current_profile_id()
        if profile_id is not None:
            self.remove_requested.emit(profile_id)

    def show_connection_state(self, *, online: bool, latency_ms: int | None = None) -> None:
        if online:
            latency = "" if latency_ms is None else f" · {latency_ms} ms"
            self.status_label.setText("● 已连接" + latency)
            self.status_label.setProperty("taskState", "recovered")
            self.connect_button.setText("重新连接")
        else:
            self.status_label.setText("● 未连接")
            self.status_label.setProperty("taskState", "warning")
            self.connect_button.setText("连接 NAS")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def show_capacity(self, free_bytes: int | None) -> None:
        self.capacity_label.setText(
            "容量尚未查询" if free_bytes is None else f"剩余可用 {_size_text(free_bytes)}"
        )

    def set_compact(self, compact: bool) -> None:
        self.is_compact = compact
        self.setFixedWidth(64 if compact else 200)
        for widget in (
            self.heading_label,
            self.protocol_label,
            self.status_label,
            self.capacity_label,
            self.connect_button,
            self.profile_list,
            self.remove_button,
        ):
            widget.setVisible(not compact)
        self.name_label.setText("NAS" if compact else self._display_name)
        self.add_button.setText("＋" if compact else "＋ 新增")
        self.add_button.setToolTip("新增 NAS 配置")
        if compact:
            self.add_button.setFixedWidth(36)
            self.edit_button.setFixedWidth(36)
        else:
            self.add_button.setMinimumWidth(0)
            self.add_button.setMaximumWidth(16_777_215)
            self.edit_button.setMinimumWidth(0)
            self.edit_button.setMaximumWidth(16_777_215)
        self.edit_button.setText("⚙" if compact else "编辑配置")


__all__ = ["DeviceSidebar"]
