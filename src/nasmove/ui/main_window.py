from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from nasmove.ui.connection_page import ConnectionPage
from nasmove.ui.queue_execution_controller import QueueExecutionController
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage
from nasmove.ui.task_commands import TaskCommandService
from nasmove.ui.task_controller import TaskController
from nasmove.ui.task_creation_controller import TaskCreationController
from nasmove.ui.task_page import TaskPage


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
    ) -> None:
        super().__init__()
        self.setWindowTitle("NasMove")
        self.setMinimumSize(760, 620)
        self.pages = QStackedWidget()
        self.connection_page = ConnectionPage(
            tester=tester,
            credential_store=credential_store,
            profile_store=task_repository,
        )
        self.source_page = SourcePage()
        self.target_page = TargetPage(gateway=gateway)
        self.task_page = TaskPage()
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
        for page in (self.connection_page, self.source_page, self.target_page, self.task_page):
            self.pages.addWidget(page)
        self.back_button = QPushButton("上一步")
        self.next_button = QPushButton("下一步")
        self.back_button.clicked.connect(lambda: self._navigate(-1))
        self.next_button.clicked.connect(lambda: self._navigate(1))
        buttons = QHBoxLayout()
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        toolbar = QHBoxLayout()
        brand = QLabel("NasMove")
        brand.setStyleSheet("font-size: 22px; font-weight: 700; color: #172b4d;")
        toolbar.addWidget(brand)
        self.connection_status_label = QLabel("NAS：未测试连接")
        toolbar.addWidget(self.connection_status_label)
        toolbar.addStretch()
        self.workbench_button = QPushButton("查看任务")
        self.workbench_button.clicked.connect(lambda: self.pages.setCurrentWidget(self.task_page))
        self.new_task_button = QPushButton("＋ 新建任务")
        self.new_task_button.setObjectName("primaryAction")
        self.new_task_button.clicked.connect(lambda: self.pages.setCurrentWidget(self.connection_page))
        self.task_page.connection_requested.connect(lambda: self.pages.setCurrentWidget(self.connection_page))
        toolbar.addWidget(self.workbench_button)
        toolbar.addWidget(self.new_task_button)
        layout.addLayout(toolbar)
        self.step_label = QLabel()
        self.step_label.setObjectName("stepLabel")
        self.page_title_label = QLabel()
        self.page_title_label.setObjectName("pageTitle")
        self.page_description_label = QLabel()
        self.page_description_label.setWordWrap(True)
        self.page_description_label.setObjectName("pageDescription")
        layout.addWidget(self.step_label)
        layout.addWidget(self.page_title_label)
        layout.addWidget(self.page_description_label)
        layout.addWidget(self.pages)
        layout.addLayout(buttons)
        self.setCentralWidget(root)
        self.pages.currentChanged.connect(self._update_navigation)
        self.connection_page.report_ready.connect(self._connection_reported)
        self._update_navigation(0)
        self.pages.setCurrentWidget(self.task_page)
        self.setStyleSheet(
            """
            QMainWindow { background: #f4f6f8; }
            QWidget { font-size: 14px; }
            #stepLabel { color: #52606d; padding: 10px 4px; }
            #pageTitle { font-size: 24px; font-weight: 700; color: #172b4d; padding-top: 8px; }
            #pageDescription { color: #52606d; padding-bottom: 10px; }
            #locationLabel, #selectedPathLabel { font-weight: 600; color: #0052cc; }
            QPushButton { min-height: 30px; padding: 4px 12px; }
            QListWidget { background: white; border: 1px solid #dfe1e6; border-radius: 6px; }
            QListWidget::item { padding: 16px 10px; border-bottom: 1px solid #edf0f5; }
            QListWidget::item:selected { background: #eaf2ff; color: #164c96; }
            #taskDetail { background: white; border-radius: 12px; }
            #taskName { font-size: 20px; font-weight: 600; color: #172b4d; }
            #primaryAction { background: #175cd3; color: white; border: none; border-radius: 6px; }
            QProgressBar { border: 1px solid #dfe5ed; border-radius: 5px; min-height: 24px; text-align: center; }
            QProgressBar::chunk { background: #93c5fd; border-radius: 4px; }
            """
        )

    def _navigate(self, offset: int) -> None:
        target = max(0, min(self.pages.count() - 1, self.pages.currentIndex() + offset))
        self.pages.setCurrentIndex(target)

    def _update_navigation(self, index: int) -> None:
        titles = ("连接 NAS", "选择本地文件", "选择 NAS 目标目录", "任务队列")
        descriptions = (
            "填写或确认 NAS 登录信息，测试成功后才能浏览远端目录。",
            "添加一个或多个文件、文件夹，并选择复制或安全移动。",
            "从 NAS 目录中选择迁移目标，不需要手动填写路径。",
            "查看传输进度、重试状态和最终结果。",
        )
        steps = [
            f"{'●' if position == index else '○'} {position + 1}  {title}"
            for position, title in enumerate(titles)
        ]
        self.step_label.setText("    ".join(steps))
        self.page_title_label.setText(titles[index])
        self.page_description_label.setText(descriptions[index])
        self.back_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < self.pages.count() - 1)
        workbench = index == 3
        self.back_button.setVisible(not workbench)
        self.next_button.setVisible(not workbench and index != 2)
        self.step_label.setVisible(not workbench)
        self.workbench_button.setVisible(not workbench)
        self.page_title_label.setVisible(not workbench)
        self.page_description_label.setVisible(not workbench)
        self.next_button.setText("选择本地文件 →" if index == 0 else "选择 NAS 目标目录 →")

    def _connection_reported(self, report: object) -> None:
        if bool(getattr(report, "success", False)):
            self.connection_status_label.setText("NAS：连接测试成功")
            self.target_page.start_browsing()
        else:
            self.connection_status_label.setText("NAS：连接测试失败")

    def _task_created(self, task: object) -> None:
        self.task_controller.append_task(task)
        self.pages.setCurrentWidget(self.task_page)
        if self.queue_execution is not None:
            self.queue_execution.start()
