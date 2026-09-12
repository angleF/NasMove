from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from nasmove.core.errors import ConnectionProfileInUse
from nasmove.core.model import ConnectionConfig, ConnectionProfileId, RemotePath
from nasmove.ui.connection_page import ConnectionPage
from nasmove.ui.directory_browser_controller import DirectoryBrowserController
from nasmove.ui.directory_models import DirectorySide
from nasmove.ui.navigation import AppDestination, AppNavigation
from nasmove.ui.queue_execution_controller import QueueExecutionController
from nasmove.ui.setup_workspace import SetupWorkspace
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage
from nasmove.ui.task_commands import TaskCommandService
from nasmove.ui.task_controller import TaskController
from nasmove.ui.task_creation_controller import TaskCreationController
from nasmove.ui.task_page import TaskPage
from nasmove.ui.theme import ThemeController, ThemeName
from nasmove.ui.transfer_workspace import TransferWorkspace

_DESTINATION_TITLES = {
    AppDestination.WORKBENCH: "迁移工作台",
    AppDestination.QUEUE: "任务队列",
    AppDestination.CONNECTIONS: "账号与连接",
    AppDestination.HISTORY: "历史与报告",
    AppDestination.PREFERENCES: "偏好设置",
}


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        gateway: object | None = None,
        tester: object | None = None,
        credential_store: object | None = None,
        task_commands: object | None = None,
        task_repository: object | None = None,
        task_planner: object | None = None,
        application: object | None = None,
        queue_coordinator: object | None = None,
        profile_service: object | None = None,
        confirm_profile_removal: Callable[[ConnectionConfig], bool] | None = None,
        theme_controller: ThemeController | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("NasMove")
        self.setMinimumSize(760, 620)
        self._gateway = gateway
        self.profile_service = profile_service
        self._confirm_profile_removal = (
            confirm_profile_removal or self._confirm_profile_removal_dialog
        )
        self.theme_controller = theme_controller or ThemeController(QSettings("NasMove", "NasMove"))
        self.connection_page = ConnectionPage(
            tester=tester,
            credential_store=credential_store,
            profile_store=task_repository,
        )
        self.source_page = SourcePage()
        self.target_page = TargetPage(gateway=gateway)
        self.task_page = TaskPage()
        self.setup_workspace = SetupWorkspace(
            self.connection_page,
            self.source_page,
            self.target_page,
        )
        self.transfer_workspace = TransferWorkspace(self.source_page, self.target_page)
        self.directory_browser = DirectoryBrowserController(gateway=gateway)
        self._local_browser_started = False
        self.queue_coordinator = queue_coordinator
        self.task_commands = task_commands
        if self.task_commands is None and task_repository is not None and queue_coordinator is not None:
            self.task_commands = TaskCommandService(task_repository, queue_coordinator)
        self.task_controller = TaskController(
            self.task_page,
            commands=self.task_commands,
            repository=task_repository,
        )
        self.task_controller.attach_queue_panel(self.transfer_workspace.queue_panel)
        self.queue_execution = (
            None
            if queue_coordinator is None
            else QueueExecutionController(queue_coordinator, self.task_controller)
        )
        self.task_creation_controller: TaskCreationController | None = None
        if task_planner is not None and task_repository is not None and application is not None:
            self.task_creation_controller = TaskCreationController(
                self.connection_page,
                self.source_page,
                self.target_page,
                planner=task_planner,
                repository=task_repository,
                application=application,
                conflict_policy_provider=self.transfer_workspace.selected_conflict_policy,
            )
            self.task_creation_controller.task_created.connect(self._task_created)

        self.navigation = AppNavigation()
        self.content_stack = QStackedWidget()
        self.pages = self.content_stack
        self.content_stack.addWidget(self.transfer_workspace)
        self.content_stack.addWidget(self.task_page)
        self.content_stack.addWidget(self.connection_page)
        self.target_dialog = QDialog(self)
        self.target_dialog.setWindowTitle("选择 NAS 目标文件夹")
        self.target_dialog.resize(680, 560)
        target_layout = QVBoxLayout(self.target_dialog)
        target_layout.addWidget(self.target_page)
        self.target_page.target_changed.connect(lambda _path: self.target_dialog.accept())
        self.history_page = self._message_page(
            "历史与报告",
            "已完成、暂停和失败任务保留在任务队列中。选择任务后可查看文件结果或导出脱敏摘要。",
        )
        self.preferences_page = self._message_page(
            "偏好设置",
            "使用右上角主题菜单在雾蓝玻璃、深夜运维和暖灰工作室之间切换。",
        )
        self.content_stack.addWidget(self.history_page)
        self.content_stack.addWidget(self.preferences_page)
        self._build_ui()
        self._connect_signals()
        self._reload_profiles()
        self._apply_theme(self.theme_controller.selected_theme())
        self.show_destination(AppDestination.WORKBENCH)
        self.connection_page.test_button.setText("连接并进入文件迁移")
        if self.connection_page.profile_id is not None:
            self.connection_status_label.setText("NAS：正在恢复连接…")
            QTimer.singleShot(0, self.connection_page.test_connection)

    def _choose_target(self) -> None:
        if not self.setup_workspace.connection_verified:
            self.show_destination(AppDestination.CONNECTIONS)
            return
        self.target_dialog.open()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.navigation.setFixedWidth(184)
        layout.addWidget(self.navigation)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 16, 20, 16)
        content_layout.setSpacing(12)
        toolbar = QHBoxLayout()
        self.page_title_label = QLabel()
        self.page_title_label.setObjectName("pageTitle")
        self.connection_status_label = QLabel("NAS：未测试连接")
        self.connection_status_label.setProperty("themeRole", "muted")
        self.theme_selector = QComboBox()
        self.theme_selector.setObjectName("themeSelector")
        self.theme_selector.addItem("跟随系统", ThemeName.SYSTEM)
        self.theme_selector.addItem("雾蓝玻璃", ThemeName.FROSTED_LIGHT)
        self.theme_selector.addItem("深夜运维", ThemeName.MIDNIGHT_OPS)
        self.theme_selector.addItem("暖灰工作室", ThemeName.WARM_STUDIO)
        self._sync_theme_selector(self.theme_controller.selected_theme())
        self.new_task_button = QPushButton("＋ 新建迁移")
        self.new_task_button.setProperty("themeRole", "primary")
        toolbar.addWidget(self.page_title_label)
        toolbar.addWidget(self.connection_status_label)
        toolbar.addStretch()
        toolbar.addWidget(self.theme_selector)
        toolbar.addWidget(self.new_task_button)
        content_layout.addLayout(toolbar)
        content_layout.addWidget(self.content_stack, 1)
        layout.addWidget(content, 1)
        self.setCentralWidget(root)

    def _connect_signals(self) -> None:
        self.navigation.destination_requested.connect(self.show_destination)
        self.navigation.activity_requested.connect(lambda: self.show_destination(AppDestination.QUEUE))
        self.new_task_button.clicked.connect(lambda: self.show_destination(AppDestination.WORKBENCH))
        self.task_page.connection_requested.connect(
            lambda: self.show_destination(AppDestination.CONNECTIONS)
        )
        self.task_page.workbench_requested.connect(
            lambda: self.show_destination(AppDestination.WORKBENCH)
        )
        self.connection_page.report_ready.connect(self._connection_reported)
        self.connection_page.configuration_changed.connect(self._connection_changed)
        self.setup_workspace.edit_connection_requested.connect(
            lambda: self.show_destination(AppDestination.CONNECTIONS)
        )
        self.setup_workspace.edit_target_requested.connect(self._choose_target)
        self.transfer_workspace.edit_connection_requested.connect(
            lambda: self.show_destination(AppDestination.CONNECTIONS)
        )
        self.transfer_workspace.connect_requested.connect(
            lambda: self.show_destination(AppDestination.CONNECTIONS)
        )
        self.transfer_workspace.device_sidebar.add_requested.connect(self._new_profile)
        self.transfer_workspace.device_sidebar.profile_selected.connect(
            self._select_profile
        )
        self.transfer_workspace.device_sidebar.remove_requested.connect(
            self._remove_profile
        )
        self.transfer_workspace.create_task_requested.connect(
            self.target_page.create_task_requested.emit
        )
        self.transfer_workspace.task_details_requested.connect(
            lambda _task_id: self.show_destination(AppDestination.QUEUE)
        )
        if self.task_creation_controller is not None:
            self.task_creation_controller.creating_changed.connect(self.setup_workspace.set_creating)
            self.task_creation_controller.creating_changed.connect(
                self.transfer_workspace.set_creating
            )
        self.task_controller.workspace_state_changed.connect(self._show_task_workspace)
        self.directory_browser.snapshot_ready.connect(
            self.transfer_workspace.apply_directory_snapshot
        )
        self.directory_browser.loading_changed.connect(self._directory_loading_changed)
        self.directory_browser.error_code.connect(self._directory_error)
        self.transfer_workspace.local_pane.directory_requested.connect(
            lambda entry: self.directory_browser.browse_local(Path(entry.identity))
        )
        self.transfer_workspace.local_pane.parent_requested.connect(self._browse_local_parent)
        self.transfer_workspace.local_pane.refresh_requested.connect(self._refresh_local)
        self.transfer_workspace.local_pane.new_folder_requested.connect(self._create_local_folder)
        self.transfer_workspace.remote_pane.directory_requested.connect(
            lambda entry: self._browse_remote(entry.identity)
        )
        self.transfer_workspace.remote_pane.parent_requested.connect(self._browse_remote_parent)
        self.transfer_workspace.remote_pane.refresh_requested.connect(self._refresh_remote)
        self.transfer_workspace.remote_pane.new_folder_requested.connect(self._create_remote_folder)
        self.theme_selector.currentIndexChanged.connect(self._select_theme)
        self.theme_controller.theme_changed.connect(self._apply_theme)

    def show_destination(self, destination: AppDestination) -> None:
        if destination is AppDestination.CONNECTIONS:
            if self.queue_execution is not None and self.queue_execution.running:
                self.connection_status_label.setText("任务处理中，请暂停任务后切换账号")
                return
            if not self.source_page.isEnabled():
                self.connection_status_label.setText("任务创建中，请稍后切换账号")
                return
        self.navigation.set_selected(destination)
        self.page_title_label.setText(_DESTINATION_TITLES[destination])
        if destination is AppDestination.WORKBENCH:
            self.content_stack.setCurrentWidget(self.transfer_workspace)
        elif destination is AppDestination.QUEUE:
            self.content_stack.setCurrentWidget(self.task_page)
        elif destination is AppDestination.CONNECTIONS:
            self.content_stack.setCurrentWidget(self.connection_page)
        elif destination is AppDestination.HISTORY:
            self.content_stack.setCurrentWidget(self.history_page)
        else:
            self.content_stack.setCurrentWidget(self.preferences_page)

    def _select_theme(self, index: int) -> None:
        selected = self.theme_selector.itemData(index)
        if selected is not None:
            self.theme_controller.select(ThemeName(str(selected)))

    def _apply_theme(self, theme: object) -> None:
        selected = theme if isinstance(theme, ThemeName) else ThemeName(str(theme))
        self._sync_theme_selector(selected)
        self.setStyleSheet(self.theme_controller.stylesheet())

    def _sync_theme_selector(self, theme: ThemeName) -> None:
        self.theme_selector.blockSignals(True)
        for index in range(self.theme_selector.count()):
            if self.theme_selector.itemData(index) == theme:
                self.theme_selector.setCurrentIndex(index)
                break
        self.theme_selector.blockSignals(False)

    def _connection_reported(self, report: object) -> None:
        if bool(getattr(report, "success", False)):
            self.connection_status_label.setText("NAS：连接测试成功")
            config = self.connection_page.connection_config()
            self.transfer_workspace.device_sidebar.show_profile(config)
            self._reload_profiles(preferred_profile_id=config.profile_id)
            self.transfer_workspace.set_connection_verified(True)
            self.target_page.start_browsing()
            if self.directory_browser.can_browse_remote:
                self.directory_browser.browse_remote(None, str(config.profile_id))
            if self.content_stack.currentWidget() is self.connection_page:
                self.show_destination(AppDestination.WORKBENCH)
        else:
            self.connection_status_label.setText("NAS：连接测试失败")
            self.transfer_workspace.set_connection_verified(False)

    def _connection_changed(self) -> None:
        self._invalidate_connection_context("NAS：账号已更改，请连接")

    def _profile_change_block_reason(self) -> str | None:
        if self.connection_page.is_testing:
            return "连接测试中，请稍后切换账号"
        if not self.source_page.isEnabled():
            return "任务创建中，请稍后切换账号"
        if self.queue_execution is not None and self.queue_execution.running:
            return "任务处理中，请暂停任务后切换账号"
        return None

    def _reload_profiles(
        self, *, preferred_profile_id: ConnectionProfileId | None = None
    ) -> None:
        service = self.profile_service
        listing = getattr(service, "list_profiles", None)
        if not callable(listing):
            return
        try:
            profiles = tuple(listing())
        except Exception:  # noqa: BLE001 - storage details stay out of the UI
            self.connection_status_label.setText("NAS：无法读取连接配置")
            return
        available = {config.profile_id for config in profiles}
        selected = preferred_profile_id or self.connection_page.profile_id
        if selected not in available:
            selected = profiles[0].profile_id if profiles else None
        self.transfer_workspace.device_sidebar.set_profiles(
            profiles,
            selected_profile_id=selected,
            select_first=selected is not None,
        )
        if selected is None:
            if not self.connection_page.is_testing:
                self.connection_page.clear_profile()
            return
        if self.connection_page.profile_id != selected:
            getter = getattr(service, "get_profile", None)
            if callable(getter) and not self.connection_page.is_testing:
                self.connection_page.load_profile(getter(selected))

    def _restore_profile_selection(self) -> None:
        self._reload_profiles(preferred_profile_id=self.connection_page.profile_id)

    def _select_profile(self, value: object) -> None:
        profile_id = ConnectionProfileId(str(value))
        if profile_id == self.connection_page.profile_id:
            return
        blocked = self._profile_change_block_reason()
        if blocked is not None:
            self.connection_status_label.setText(blocked)
            self._restore_profile_selection()
            return
        getter = getattr(self.profile_service, "get_profile", None)
        if not callable(getter):
            return
        try:
            config = getter(profile_id)
        except Exception:  # noqa: BLE001 - storage details stay out of the UI
            self.connection_status_label.setText("NAS：无法读取所选配置")
            self._restore_profile_selection()
            return
        self._invalidate_connection_context("NAS：配置已切换，请连接")
        self.connection_page.load_profile(config)
        self._reload_profiles(preferred_profile_id=profile_id)

    def _new_profile(self) -> None:
        blocked = self._profile_change_block_reason()
        if blocked is not None:
            self.connection_status_label.setText(blocked)
            return
        self._invalidate_connection_context("NAS：请填写并测试新配置")
        self.connection_page.clear_profile()
        listing = getattr(self.profile_service, "list_profiles", None)
        if callable(listing):
            try:
                profiles = tuple(listing())
            except Exception:  # noqa: BLE001 - storage details stay out of the UI
                profiles = ()
            self.transfer_workspace.device_sidebar.set_profiles(
                profiles, select_first=False
            )
        self.show_destination(AppDestination.CONNECTIONS)

    def _remove_profile(self, value: object) -> None:
        profile_id = ConnectionProfileId(str(value))
        blocked = self._profile_change_block_reason()
        if blocked is not None:
            self.connection_status_label.setText(blocked)
            self._restore_profile_selection()
            return
        getter = getattr(self.profile_service, "get_profile", None)
        archiver = getattr(self.profile_service, "archive", None)
        if not callable(getter) or not callable(archiver):
            return
        try:
            config = getter(profile_id)
        except Exception:  # noqa: BLE001 - storage details stay out of the UI
            self.connection_status_label.setText("NAS：配置不存在或已移除")
            self._reload_profiles()
            return
        if not self._confirm_profile_removal(config):
            self._restore_profile_selection()
            return
        try:
            result = archiver(profile_id)
        except ConnectionProfileInUse:
            self.connection_status_label.setText(
                "NAS：配置仍被未完成任务使用，暂时不能移除"
            )
            self._restore_profile_selection()
            return
        except Exception:  # noqa: BLE001 - storage details stay out of the UI
            self.connection_status_label.setText("NAS：配置移除失败")
            self._restore_profile_selection()
            return
        self._invalidate_connection_context("NAS：配置已移除")
        self._reload_profiles()
        if getattr(result, "warning_code", None) == "credential_cleanup_failed":
            self.connection_status_label.setText(
                "NAS：配置已移除，但本机凭据清理失败"
            )

    def _invalidate_connection_context(self, message: str) -> None:
        self.setup_workspace.set_connection_verified(False)
        self.transfer_workspace.set_connection_verified(False)
        self.directory_browser.invalidate(DirectorySide.REMOTE)
        reset = getattr(self._gateway, "reset_connection", None)
        reset_failed = False
        if callable(reset):
            try:
                reset()
            except Exception:  # noqa: BLE001 - stale sessions remain unusable in the UI
                reset_failed = True
        self.transfer_workspace.clear_remote()
        self.transfer_workspace.device_sidebar.show_capacity(None)
        self.target_page.clear_selection()
        self.connection_status_label.setText(
            message
            if not reset_failed
            else message + "；旧连接清理失败，请重启应用后重试"
        )

    def _confirm_profile_removal_dialog(self, config: ConnectionConfig) -> bool:
        answer = QMessageBox.question(
            self,
            "移除 NAS 配置",
            f"确定移除“{config.display_name}”及其已保存密码吗？任务历史不会删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _show_task_workspace(self, state: object) -> None:
        active_states = {"running", "verifying", "committing", "deleting_source", "waiting_for_network"}
        if (
            self.content_stack.currentWidget() is self.task_page
            and str(getattr(state, "value", state)) in active_states
        ):
            self.navigation.set_selected(AppDestination.QUEUE)
            self.page_title_label.setText(_DESTINATION_TITLES[AppDestination.QUEUE])
            self.content_stack.setCurrentWidget(self.task_page)
        self._update_active_task_count()

    def _task_created(self, task: object) -> None:
        self.source_page.set_sources([])
        self.transfer_workspace.local_pane.table.clearSelection()
        self.task_controller.append_task(task)
        self._update_active_task_count()
        if self.queue_execution is not None:
            self.queue_execution.start()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._local_browser_started:
            self._local_browser_started = True
            self.directory_browser.browse_local(Path.home())
        self.transfer_workspace.apply_responsive_width(self.width())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.transfer_workspace.apply_responsive_width(self.width())

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.directory_browser.shutdown():
            event.ignore()
            self.connection_status_label.setText("正在停止目录读取，请稍后关闭")
            return
        coordinator = self.queue_coordinator
        shutdown = getattr(coordinator, "shutdown", None)
        if callable(shutdown) and not shutdown():
            event.ignore()
            self.connection_status_label.setText("正在停止传输，请稍后关闭")
            return
        super().closeEvent(event)

    def _browse_local_parent(self) -> None:
        location = self.transfer_workspace.local_pane.location
        if location:
            self.directory_browser.browse_local(Path(location).parent)

    def _browse_remote(self, value: str) -> None:
        profile_id = self.connection_page.profile_id
        if profile_id is not None:
            self.directory_browser.browse_remote(RemotePath(value), str(profile_id))

    def _browse_remote_parent(self) -> None:
        profile_id = self.connection_page.profile_id
        if profile_id is None:
            return
        location = self.transfer_workspace.remote_pane.location
        parent_value = location.rpartition("/")[0]
        parent = RemotePath(parent_value) if parent_value else None
        self.directory_browser.browse_remote(parent, str(profile_id))

    def _refresh_local(self) -> None:
        location = self.transfer_workspace.local_pane.location
        if location:
            self.directory_browser.browse_local(Path(location))

    def _refresh_remote(self) -> None:
        profile_id = self.connection_page.profile_id
        if profile_id is not None:
            location = self.transfer_workspace.remote_pane.location
            target = RemotePath(location) if location else None
            self.directory_browser.browse_remote(target, str(profile_id))

    def _create_local_folder(self, name: str) -> None:
        location = self.transfer_workspace.local_pane.location
        if location:
            target = Path(location) / name
            try:
                target.mkdir(parents=False, exist_ok=False)
                self.directory_browser.browse_local(Path(location))
            except Exception as exc:
                self.connection_status_label.setText(f"新建本地文件夹失败：{exc}")

    def _create_remote_folder(self, name: str) -> None:
        profile_id = self.connection_page.profile_id
        if profile_id is None:
            return
        location = self.transfer_workspace.remote_pane.location
        new_path_str = f"{location}/{name}" if location else name
        self.directory_browser.make_remote_dir(
            RemotePath(new_path_str), str(profile_id), location
        )

    def _directory_loading_changed(self, payload: object) -> None:
        side, loading = cast(tuple[DirectorySide, bool], payload)
        pane = (
            self.transfer_workspace.local_pane
            if side is DirectorySide.LOCAL
            else self.transfer_workspace.remote_pane
        )
        pane.set_loading(bool(loading))

    def _directory_error(self, payload: object) -> None:
        side, code = cast(tuple[DirectorySide, str], payload)
        pane_name = "Mac" if side is DirectorySide.LOCAL else "NAS"
        self.connection_status_label.setText(f"{pane_name} 目录读取失败：{code}")

    def _update_active_task_count(self) -> None:
        active_states = {
            "queued",
            "running",
            "verifying",
            "committing",
            "deleting_source",
            "waiting_for_network",
        }
        count = sum(
            1
            for row in range(self.task_page.queue_list.count())
            if str(self.task_page.queue_list.item(row).data(Qt.ItemDataRole.UserRole + 1))
            in active_states
        )
        self.navigation.set_active_task_count(count)

    @staticmethod
    def _message_page(title: str, message: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading = QLabel(title)
        heading.setObjectName("workspaceTitle")
        detail = QLabel(message)
        detail.setWordWrap(True)
        detail.setProperty("themeRole", "muted")
        layout.addWidget(heading)
        layout.addWidget(detail)
        return page


__all__ = ["MainWindow"]
