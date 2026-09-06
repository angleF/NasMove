from __future__ import annotations

from PySide6.QtWidgets import QMainWindow, QStackedWidget

from nasmove.ui.connection_page import ConnectionPage
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage
from nasmove.ui.task_page import TaskPage


class MainWindow(QMainWindow):
    def __init__(self, *, gateway: object | None = None, tester: object | None = None, credential_store: object | None = None) -> None:
        super().__init__()
        self.setWindowTitle("NasMove")
        self.pages = QStackedWidget()
        self.connection_page = ConnectionPage(tester=tester, credential_store=credential_store)
        self.source_page = SourcePage()
        self.target_page = TargetPage(gateway=gateway)
        self.task_page = TaskPage()
        for page in (self.connection_page, self.source_page, self.target_page, self.task_page):
            self.pages.addWidget(page)
        self.setCentralWidget(self.pages)

