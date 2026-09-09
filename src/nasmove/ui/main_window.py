from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from nasmove.ui.connection_page import ConnectionPage
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

_DESTINATION_TITLES = {
    AppDestination.WORKBENCH: "迁移工作台",
    AppDestination.QUEUE: "任务队列",
    AppDestination.CONNECTIONS: "连接配置",
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
        theme_controller: ThemeController | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("NasMove")
        self.setMinimumSize(760, 620)
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
        self.task_commands = task_commands
        if self.task_commands is None and task_repository is not None and queue_coordinator is not None:
            self.task_commands = TaskCommandService(task_repository, queue_coordinator)
        self.task_controller = TaskController(
            self.task_page,
            commands=self.task_commands,
            repository=task_repository,
        )
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
            )
            self.task_creation_controller.task_created.connect(self._task_created)

        self.navigation = AppNavigation()
        self.content_stack = QStackedWidget()
        self.pages = self.content_stack
        self.content_stack.addWidget(self.setup_workspace)
        self.content_stack.addWidget(self.task_page)
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
        self._apply_theme(self.theme_controller.selected_theme())
        self.show_destination(AppDestination.WORKBENCH)

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
        self.connection_page.report_ready.connect(self._connection_reported)
        self.task_controller.workspace_state_changed.connect(self._show_task_workspace)
        self.theme_selector.currentIndexChanged.connect(self._select_theme)
        self.theme_controller.theme_changed.connect(self._apply_theme)

    def show_destination(self, destination: AppDestination) -> None:
        self.navigation.set_selected(destination)
        self.page_title_label.setText(_DESTINATION_TITLES[destination])
        if destination is AppDestination.WORKBENCH:
            self.content_stack.setCurrentWidget(self.setup_workspace)
        elif destination is AppDestination.QUEUE:
            self.content_stack.setCurrentWidget(self.task_page)
        elif destination is AppDestination.CONNECTIONS:
            self.setup_workspace.show_connection_editor()
            self.content_stack.setCurrentWidget(self.setup_workspace)
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
            self.target_page.start_browsing()
        else:
            self.connection_status_label.setText("NAS：连接测试失败")

    def _show_task_workspace(self, state: object) -> None:
        active_states = {"running", "verifying", "committing", "deleting_source", "waiting_for_network"}
        if str(getattr(state, "value", state)) in active_states:
            self.navigation.set_selected(AppDestination.WORKBENCH)
            self.page_title_label.setText(_DESTINATION_TITLES[AppDestination.WORKBENCH])
            self.content_stack.setCurrentWidget(self.task_page)
        self._update_active_task_count()

    def _task_created(self, task: object) -> None:
        self.task_controller.append_task(task)
        self._update_active_task_count()
        self.show_destination(AppDestination.QUEUE)
        if self.queue_execution is not None:
            self.queue_execution.start()

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
