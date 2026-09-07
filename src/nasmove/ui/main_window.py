from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
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
        self.pages = QStackedWidget()
        self.connection_page = ConnectionPage(tester=tester, credential_store=credential_store)
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
        layout.addWidget(self.pages)
        layout.addLayout(buttons)
        self.setCentralWidget(root)
        self.pages.currentChanged.connect(self._update_navigation)
        self._update_navigation(0)

    def _navigate(self, offset: int) -> None:
        target = max(0, min(self.pages.count() - 1, self.pages.currentIndex() + offset))
        self.pages.setCurrentIndex(target)

    def _update_navigation(self, index: int) -> None:
        self.back_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < self.pages.count() - 1)

    def _task_created(self, task: object) -> None:
        self.task_controller.append_task(task)
        self.pages.setCurrentWidget(self.task_page)
        if self.queue_execution is not None:
            self.queue_execution.start()

