from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class SourcePage(QWidget):
    sources_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.sources: list[Path] = []
        self.preserve_hierarchy_checkbox = QCheckBox("保留源目录层级")
        self.preserve_hierarchy_checkbox.setChecked(True)
        self.move_checkbox = QCheckBox("移动（复制、校验后删除源）")
        self.verify_checkbox = QCheckBox("完整校验")
        self.verify_checkbox.setChecked(True)
        self.verify_checkbox.setEnabled(True)
        self.add_files_button = QPushButton("添加文件")
        self.add_directory_button = QPushButton("添加文件夹")
        self.remove_button = QPushButton("移除选中")
        self.clear_button = QPushButton("清空")
        self.add_button = self.add_files_button
        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.installEventFilter(self)
        self.add_files_button.clicked.connect(self.choose_files)
        self.add_directory_button.clicked.connect(self.choose_directory)
        self.remove_button.clicked.connect(self.remove_selected_sources)
        self.clear_button.clicked.connect(lambda: self.set_sources([]))
        self.move_checkbox.toggled.connect(self._move_toggled)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("选择要迁移的本地文件或文件夹，也可以直接拖入下方列表。"))
        layout.addWidget(self.preserve_hierarchy_checkbox)
        layout.addWidget(self.move_checkbox)
        layout.addWidget(self.verify_checkbox)
        buttons = QHBoxLayout()
        for button in (
            self.add_files_button,
            self.add_directory_button,
            self.remove_button,
            self.clear_button,
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addWidget(self.list_widget)
        self.setAcceptDrops(True)

    def _move_toggled(self, checked: bool) -> None:
        self.verify_checkbox.setChecked(True)
        self.verify_checkbox.setEnabled(not checked)

    def choose_sources(self) -> None:
        self.choose_files()

    def choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择源文件")
        self.add_sources([Path(value) for value in files])

    def choose_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择源目录")
        if directory:
            self.add_sources([Path(directory)])

    def add_sources(self, sources: list[Path]) -> None:
        self.set_sources([*self.sources, *sources])

    def remove_selected_sources(self) -> None:
        selected_rows = sorted(
            {self.list_widget.row(item) for item in self.list_widget.selectedItems()},
            reverse=True,
        )
        updated = list(self.sources)
        for row in selected_rows:
            if 0 <= row < len(updated):
                updated.pop(row)
        self.set_sources(updated)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.list_widget
            and isinstance(event, QEvent)
            and event.type() is QEvent.Type.KeyPress
            and cast(Any, event).key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}
        ):
            self.remove_selected_sources()
            return True
        return super().eventFilter(watched, event)

    def set_sources(self, sources: list[Path]) -> None:
        self.sources = list(dict.fromkeys(sources))
        self.list_widget.clear()
        self.list_widget.addItems([str(path) for path in self.sources])
        self.sources_changed.emit(tuple(self.sources))

    def dragEnterEvent(self, event: object) -> None:
        qt_event = cast(Any, event)
        mime = qt_event.mimeData()
        if mime is not None and getattr(mime, "hasUrls", lambda: False)():
            qt_event.acceptProposedAction()

    def dropEvent(self, event: object) -> None:
        qt_event = cast(Any, event)
        mime = qt_event.mimeData()
        urls = [] if mime is None else mime.urls()
        self.add_sources([Path(url.toLocalFile()) for url in urls if url.isLocalFile()])
        qt_event.acceptProposedAction()
