from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QFileDialog, QListWidget, QPushButton, QVBoxLayout, QWidget


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
        self.add_button = QPushButton("选择文件或目录")
        self.list_widget = QListWidget()
        self.add_button.clicked.connect(self.choose_sources)
        self.move_checkbox.toggled.connect(self._move_toggled)
        layout = QVBoxLayout(self)
        layout.addWidget(self.preserve_hierarchy_checkbox)
        layout.addWidget(self.move_checkbox)
        layout.addWidget(self.verify_checkbox)
        layout.addWidget(self.add_button)
        layout.addWidget(self.list_widget)
        self.setAcceptDrops(True)

    def _move_toggled(self, checked: bool) -> None:
        self.verify_checkbox.setChecked(True)
        self.verify_checkbox.setEnabled(not checked)

    def choose_sources(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择源文件")
        directory = QFileDialog.getExistingDirectory(self, "选择源目录")
        self.set_sources([Path(value) for value in files] + ([Path(directory)] if directory else []))

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
        self.set_sources([Path(url.toLocalFile()) for url in urls if url.isLocalFile()])
        qt_event.acceptProposedAction()
