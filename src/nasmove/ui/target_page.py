from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, cast

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from nasmove.core.errors import InvalidRemotePath
from nasmove.core.model import RemotePath
from nasmove.planning.paths import normalize_remote_path
from nasmove.ui.view_models import RemoteBrowseReport
from nasmove.ui.worker import BackgroundCommandWorker


class TargetPage(QWidget):
    browse_ready = Signal(object)
    create_task_requested = Signal()
    target_changed = Signal(object)

    def __init__(self, *, gateway: object | None = None) -> None:
        super().__init__()
        self._gateway = gateway
        self._thread: QThread | None = None
        self._worker: BackgroundCommandWorker | None = None
        self._current_path: RemotePath | None = None
        self._selected_path: RemotePath | None = None

        self.help_label = QLabel(
            "浏览 NAS 共享目录并进入目标文件夹。共享目录根层只用于导航，不能直接作为目标。"
        )
        self.help_label.setWordWrap(True)
        self.location_caption = QLabel("当前位置：")
        self.location_label = QLabel("共享目录根目录")
        self.location_label.setObjectName("locationLabel")
        self.up_button = QPushButton("返回上级")
        self.refresh_button = QPushButton("刷新")
        self.browse_button = self.refresh_button
        self.entries_list = QListWidget()
        self.entries_list.setAlternatingRowColors(True)
        self.entries_list.setToolTip("双击文件夹进入")
        self.choose_current_button = QPushButton("选择当前文件夹")
        self.choose_current_button.setEnabled(False)
        self.selected_caption = QLabel("已选择目标目录：")
        self.selected_path_label = QLabel("尚未选择")
        self.selected_path_label.setObjectName("selectedPathLabel")
        self.free_space_label = QLabel("可用空间：未查询")
        self.safety_margin_label = QLabel("安全余量：创建任务时计算")
        self.capability_label = QLabel("目录状态：等待浏览")
        self.creation_status_label = QLabel("任务：尚未创建")
        self.add_to_queue_button = QPushButton("加入队列")
        self.add_to_queue_button.setEnabled(False)

        self.up_button.clicked.connect(self.go_up)
        self.refresh_button.clicked.connect(self.browse)
        self.entries_list.itemDoubleClicked.connect(self.enter_directory)
        self.choose_current_button.clicked.connect(self.choose_current_directory)
        self.add_to_queue_button.clicked.connect(self.create_task_requested.emit)

        location = QHBoxLayout()
        location.addWidget(self.location_caption)
        location.addWidget(self.location_label, 1)
        location.addWidget(self.up_button)
        location.addWidget(self.refresh_button)
        selected = QHBoxLayout()
        selected.addWidget(self.selected_caption)
        selected.addWidget(self.selected_path_label, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.help_label)
        layout.addLayout(location)
        layout.addWidget(self.entries_list, 1)
        layout.addWidget(QLabel("提示：双击文件夹进入，再点击“选择当前文件夹”。"))
        layout.addWidget(self.choose_current_button)
        layout.addLayout(selected)
        layout.addWidget(self.free_space_label)
        layout.addWidget(self.safety_margin_label)
        layout.addWidget(self.capability_label)
        layout.addWidget(self.creation_status_label)
        layout.addWidget(self.add_to_queue_button)
        self._update_navigation_state()

    def target_path(self) -> RemotePath:
        if self._selected_path is None:
            raise InvalidRemotePath("请先选择 NAS 目标目录")
        return self._selected_path

    def set_selected_path(self, value: str | RemotePath) -> None:
        path = value if isinstance(value, RemotePath) else normalize_remote_path(value)
        self._current_path = path
        self._selected_path = path
        self.location_label.setText(path.value)
        self.selected_path_label.setText(path.value)
        self.add_to_queue_button.setEnabled(True)
        self.target_changed.emit(path)
        self._update_navigation_state()

    def start_browsing(self) -> None:
        self._current_path = None
        self.browse()

    def browse(self) -> None:
        if self._gateway is None or self._thread is not None:
            return
        self.capability_label.setText("目录状态：正在读取…")
        gateway = self._gateway
        path = self._current_path
        worker = BackgroundCommandWorker(lambda: self._read_remote(gateway, path))
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self.show_directory)
        worker.failed.connect(self._show_browse_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._worker, self._thread = worker, thread
        thread.start()

    @staticmethod
    def _read_remote(gateway: object, path: RemotePath | None) -> RemoteBrowseReport:
        smb_gateway = cast(Any, gateway)
        if path is None:
            entries = tuple(smb_gateway.list_share_root())
            free_space = smb_gateway.free_space_share_root()
        else:
            entries = tuple(smb_gateway.list_dir(path))
            free_space = smb_gateway.free_space(path)
        return RemoteBrowseReport(path, entries, free_space)

    @Slot(object)
    def show_directory(self, report: object) -> None:
        if not isinstance(report, RemoteBrowseReport):
            return
        self._current_path = report.path if isinstance(report.path, RemotePath) else None
        self.entries_list.clear()
        directories = sorted(
            (entry for entry in report.entries if getattr(entry, "is_directory", False)),
            key=lambda entry: str(getattr(entry, "name", "")).casefold(),
        )
        for entry in directories:
            item = QListWidgetItem("📁  " + str(getattr(entry, "name", "")))
            item.setData(Qt.ItemDataRole.UserRole, str(getattr(entry, "name", "")))
            self.entries_list.addItem(item)
        self.location_label.setText(
            "共享目录根目录" if self._current_path is None else self._current_path.value
        )
        if report.free_space is not None:
            self.free_space_label.setText(f"可用空间：{self._format_bytes(report.free_space)}")
        self.capability_label.setText(f"目录状态：已读取，共 {len(directories)} 个文件夹")
        self._update_navigation_state()
        self.browse_ready.emit(report)

    @Slot(object)
    def enter_directory(self, item: object) -> None:
        name = cast(Any, item).data(Qt.ItemDataRole.UserRole)
        if not isinstance(name, str) or not name:
            return
        value = name if self._current_path is None else f"{self._current_path.value}/{name}"
        self._current_path = normalize_remote_path(value)
        self.browse()

    @Slot()
    def go_up(self) -> None:
        if self._current_path is None:
            return
        parent = PurePosixPath(self._current_path.value).parent
        self._current_path = None if str(parent) == "." else RemotePath(parent.as_posix())
        self.browse()

    @Slot()
    def choose_current_directory(self) -> None:
        if self._current_path is None:
            return
        self._selected_path = self._current_path
        self.selected_path_label.setText(self._current_path.value)
        self.add_to_queue_button.setEnabled(True)
        self.target_changed.emit(self._current_path)

    def _update_navigation_state(self) -> None:
        self.up_button.setEnabled(self._current_path is not None)
        self.choose_current_button.setEnabled(self._current_path is not None)

    @Slot(str)
    def _show_browse_error(self, _error: str) -> None:
        self.capability_label.setText("目录状态：读取失败，请检查 NAS 连接或目录权限")

    @staticmethod
    def _format_bytes(value: int) -> str:
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @Slot()
    def _thread_finished(self) -> None:
        self._worker = None
        self._thread = None


__all__ = ["TargetPage"]
