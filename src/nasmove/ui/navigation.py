from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class AppDestination(StrEnum):
    WORKBENCH = "workbench"
    QUEUE = "queue"
    CONNECTIONS = "connections"
    HISTORY = "history"
    PREFERENCES = "preferences"


_DESTINATION_TEXT = {
    AppDestination.WORKBENCH: "迁移工作台",
    AppDestination.QUEUE: "任务队列",
    AppDestination.CONNECTIONS: "连接配置",
    AppDestination.HISTORY: "历史与报告",
    AppDestination.PREFERENCES: "偏好设置",
}


class AppNavigation(QWidget):
    destination_requested = Signal(object)
    activity_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("appNavigation")
        self.buttons: dict[AppDestination, QPushButton] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(4)

        brand = QLabel("NasMove")
        brand.setObjectName("navigationBrand")
        layout.addWidget(brand)
        layout.addSpacing(12)

        for destination, text in _DESTINATION_TEXT.items():
            if destination is AppDestination.HISTORY:
                layout.addSpacing(16)
            button = QPushButton(text)
            button.setObjectName(f"navigation-{destination.value}")
            button.setAccessibleName(text)
            button.clicked.connect(
                lambda _checked=False, requested=destination: self._request_destination(requested)
            )
            self.buttons[destination] = button
            layout.addWidget(button)

        layout.addStretch()
        self.activity_button = QPushButton()
        self.activity_button.setObjectName("activeTaskButton")
        self.activity_button.setAccessibleName("查看正在处理的任务")
        self.activity_button.clicked.connect(self.activity_requested.emit)
        self.activity_button.hide()
        layout.addWidget(self.activity_button)
        self.set_selected(AppDestination.WORKBENCH)

    def set_active_task_count(self, count: int) -> None:
        self.activity_button.setVisible(count > 0)
        self.activity_button.setText(f"{count} 个任务正在处理")

    def set_selected(self, destination: AppDestination) -> None:
        for candidate, button in self.buttons.items():
            button.setProperty("themeRole", "selected" if candidate is destination else "")
            self._repolish(button)

    def _request_destination(self, destination: AppDestination) -> None:
        self.set_selected(destination)
        self.destination_requested.emit(destination)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


__all__ = ["AppDestination", "AppNavigation"]
