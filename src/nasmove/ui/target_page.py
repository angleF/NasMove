from __future__ import annotations

from typing import Any, cast

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtWidgets import QLabel, QLineEdit, QListWidget, QPushButton, QVBoxLayout, QWidget

from nasmove.planning.paths import normalize_remote_path
from nasmove.ui.view_models import RemoteBrowseReport
from nasmove.ui.worker import BackgroundCommandWorker


class TargetPage(QWidget):
    browse_ready = Signal(object)

    def __init__(self, *, gateway: object | None = None) -> None:
        super().__init__()
        self._gateway = gateway
        self._thread: QThread | None = None
        self._worker: BackgroundCommandWorker | None = None
        self.path_lineedit = QLineEdit()
        self.browse_button = QPushButton("浏览")
        self.entries_list = QListWidget()
        self.free_space_label = QLabel("可用空间：未查询")
        self.safety_margin_label = QLabel("安全余量：—")
        self.capability_label = QLabel("SMB 能力：未探测")
        self.browse_button.clicked.connect(self.browse)
        layout = QVBoxLayout(self)
        layout.addWidget(self.path_lineedit)
        layout.addWidget(self.browse_button)
        layout.addWidget(self.entries_list)
        layout.addWidget(self.free_space_label)
        layout.addWidget(self.safety_margin_label)
        layout.addWidget(self.capability_label)

    def browse(self) -> None:
        if self._gateway is None:
            return
        try:
            path = normalize_remote_path(self.path_lineedit.text().strip())
        except Exception as error:  # noqa: BLE001
            self.capability_label.setText("路径错误：" + str(error))
            return
        gateway = self._gateway
        worker = BackgroundCommandWorker(lambda: self._read_remote(gateway, path))
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._show_report)
        worker.failed.connect(lambda error: self.capability_label.setText("浏览失败：" + error))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._worker, self._thread = worker, thread
        thread.start()

    @staticmethod
    def _read_remote(gateway: object, path: object) -> RemoteBrowseReport:
        smb_gateway = cast(Any, gateway)
        entries = tuple(smb_gateway.list_dir(path))
        free_space = smb_gateway.free_space(path)
        return RemoteBrowseReport(path, entries, free_space)

    @Slot(object)
    def _show_report(self, report: object) -> None:
        if not isinstance(report, RemoteBrowseReport):
            return
        self.entries_list.clear()
        self.entries_list.addItems([getattr(entry, "name", str(entry)) for entry in report.entries])
        if report.free_space is not None:
            self.free_space_label.setText(f"可用空间：{report.free_space} bytes")
            self.safety_margin_label.setText("安全余量：由任务规划器计算")
        self.browse_ready.emit(report)

    @Slot()
    def _thread_finished(self) -> None:
        self._worker = None
        self._thread = None
