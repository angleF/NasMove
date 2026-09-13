from pathlib import Path

from PySide6.QtCore import QItemSelection, QTimer, Qt, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QKeyEvent,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from nasmove.ui.directory_models import (
    DirectoryEntryViewModel,
    DirectorySide,
    DirectorySnapshot,
    DirectoryTableModel,
)


class _FileTableView(QTableView):
    enter_pressed = Signal()
    parent_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        modifiers = event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.enter_pressed.emit()
            event.accept()
            return
        if (
            (
                key == Qt.Key.Key_Up
                and bool(
                    modifiers
                    & (
                        Qt.KeyboardModifier.ControlModifier
                        | Qt.KeyboardModifier.MetaModifier
                    )
                )
            )
            or key == Qt.Key.Key_Backspace
        ):
            self.parent_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class FilePane(QWidget):
    directory_requested = Signal(object)
    parent_requested = Signal()
    selection_changed = Signal(object)
    connect_requested = Signal()
    refresh_requested = Signal()
    new_folder_requested = Signal(str)

    def __init__(self, side: DirectorySide, title: str) -> None:
        super().__init__()
        self.side = side
        self.model = DirectoryTableModel(side)
        self.location = ""
        self._connection_guidance = False
        self.title_label = QLabel(title)
        self.title_label.setObjectName("filePaneTitle")
        self.up_button = QPushButton("↑")
        self.up_button.setObjectName("filePaneUpButton")
        self.up_button.setAccessibleName("返回上一级")
        self.up_button.setToolTip("返回上一级")
        self.refresh_button = QPushButton("⟳")
        self.refresh_button.setObjectName("filePaneRefreshButton")
        self.refresh_button.setAccessibleName("刷新目录")
        self.refresh_button.setToolTip("刷新目录")
        self.new_folder_button = QPushButton("+ 文件夹")
        self.new_folder_button.setObjectName("filePaneNewFolderButton")
        self.new_folder_button.setAccessibleName("新建文件夹")
        self.new_folder_button.setToolTip("在此目录下新建文件夹")
        self.path_label = QLabel("—")
        self.path_label.setObjectName("filePanePath")
        self.search_input = QLineEdit()
        self.search_edit = self.search_input
        self.search_input.setPlaceholderText("搜索文件与文件夹")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setAccessibleName(f"搜索{title}文件与文件夹")
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._apply_search_filter)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        self.search_input.returnPressed.connect(self.flush_search)
        self.table = _FileTableView()
        self.table.setObjectName("fileTable")
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
            if side is DirectorySide.LOCAL
            else QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().hide()
        table_header = self.table.horizontalHeader()
        table_header.setSortIndicatorShown(True)
        table_header.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table_header.resizeSection(1, 65)
        table_header.resizeSection(2, 80)
        table_header.setHighlightSections(False)
        self.empty_label = QLabel("这个文件夹是空的")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        self.connect_button = QPushButton("连接 / 配置 NAS")
        self.connect_button.setObjectName("filePaneConnectButton")
        self.connect_button.setProperty("themeRole", "primary")
        self.connect_button.hide()
        self.connect_button.clicked.connect(self.connect_requested.emit)
        self.loading_label = QLabel("正在读取目录…")
        self.loading_label.setProperty("themeRole", "muted")
        self.loading_label.hide()

        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        header.setSpacing(6)
        header.addWidget(self.up_button)
        header.addWidget(self.refresh_button)
        header.addWidget(self.new_folder_button)
        header.addWidget(self.title_label)
        header.addWidget(self.path_label, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self.search_input)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.connect_button)
        layout.addWidget(self.loading_label)

        self.up_button.clicked.connect(self.parent_requested.emit)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.new_folder_button.clicked.connect(self._prompt_new_folder)
        self.table.doubleClicked.connect(self._activate)
        self.table.enter_pressed.connect(self._on_table_enter)
        self.table.parent_requested.connect(self.parent_requested.emit)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.model.entries_updated.connect(self._on_model_updated)
        self.setAcceptDrops(True)

    @property
    def search_box(self) -> QLineEdit:
        return self.search_input

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_column_visibility()

    def _update_column_visibility(self) -> None:
        narrow = self.width() < 320
        self.table.setColumnHidden(2, narrow)

    def set_connection_guidance(self, show: bool) -> None:
        self._connection_guidance = show
        if show:
            self.empty_label.setText("尚未连接 NAS 设备")
            self.empty_label.setVisible(True)
            self.connect_button.setVisible(True)
            self.table.setVisible(False)
        else:
            self.connect_button.setVisible(False)
            self._sync_view_state()

    def _sync_view_state(self) -> None:
        if self._connection_guidance:
            self.empty_label.setText("尚未连接 NAS 设备")
            self.empty_label.setVisible(True)
            self.connect_button.setVisible(True)
            self.table.setVisible(False)
            return

        self.connect_button.setVisible(False)
        if self.loading_label.isVisible():
            return

        snapshot_entries = self.model.snapshot.entries
        if len(snapshot_entries) == 0:
            self.empty_label.setText("这个文件夹是空的")
            self.empty_label.setVisible(True)
            self.table.setVisible(False)
        elif self.model.rowCount() == 0:
            self.empty_label.setText("没有匹配结果")
            self.empty_label.setVisible(True)
            self.table.setVisible(False)
        else:
            self.empty_label.setVisible(False)
            self.table.setVisible(True)

    def _on_search_text_changed(self, text: str) -> None:
        if not text or self._search_timer.interval() == 0:
            self._search_timer.stop()
            self._apply_search_filter()
        else:
            self._search_timer.start()

    def flush_search(self) -> None:
        if self._search_timer.isActive():
            self._search_timer.stop()
        self._apply_search_filter()

    def _apply_search_filter(self) -> None:
        self.model.set_filter(self.search_input.text())

    def _on_model_updated(self) -> None:
        selection_model = self.table.selectionModel()
        if selection_model is not None:
            selection_model.clearSelection()
        self.set_loading(False)
        self._sync_view_state()
        self.selection_changed.emit(self.selected_entries())

    def _on_table_enter(self) -> None:
        index = self.table.currentIndex()
        if index.isValid():
            self._activate(index)

    def _prompt_new_folder(self) -> None:
        folder_name, ok = QInputDialog.getText(
            self,
            "新建文件夹",
            "请输入新文件夹名称：",
            QLineEdit.EchoMode.Normal,
            "",
        )
        if ok and folder_name.strip():
            cleaned = folder_name.strip()
            if "/" in cleaned or "\\" in cleaned or "\0" in cleaned:
                QMessageBox.warning(self, "名称无效", "文件夹名称不能包含斜杠等非法字符。")
                return
            self.new_folder_requested.emit(cleaned)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        event.acceptProposedAction()
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if not paths:
            return
        first = paths[0]
        if self.side is DirectorySide.LOCAL:
            target_dir = first if first.is_dir() else first.parent
            self.directory_requested.emit(
                DirectoryEntryViewModel(
                    name=target_dir.name,
                    is_directory=True,
                    size=0,
                    modified_ns=None,
                    identity=str(target_dir.absolute()),
                )
            )

    def apply_snapshot(self, snapshot: DirectorySnapshot) -> None:
        if snapshot.side is not self.side:
            return
        self.location = snapshot.location
        self.path_label.setText(snapshot.location or ("Mac" if self.side is DirectorySide.LOCAL else "NAS"))
        self.model.replace(snapshot)

    def selected_entries(self) -> tuple[DirectoryEntryViewModel, ...]:
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return ()
        rows = sorted({index.row() for index in selection_model.selectedRows()})
        valid_rows = [r for r in rows if 0 <= r < self.model.rowCount()]
        return tuple(self.model.entry_at(row) for row in valid_rows)

    def select_identity(self, identity: str) -> None:
        for row in range(self.model.rowCount()):
            if self.model.entry_at(row).identity == identity:
                self.table.selectRow(row)
                return

    def set_loading(self, loading: bool) -> None:
        self.loading_label.setVisible(loading)
        self.table.setEnabled(not loading)
        if loading:
            self.empty_label.hide()
            self.connect_button.hide()
        else:
            self._sync_view_state()
        self.setAccessibleDescription("正在读取目录" if loading else "目录读取完成")

    def _activate(self, index: object) -> None:
        row = getattr(index, "row", lambda: -1)()
        if 0 <= row < self.model.rowCount():
            entry = self.model.entry_at(row)
            if entry.is_directory:
                self.directory_requested.emit(entry)

    def _selection_changed(self, _selected: QItemSelection, _deselected: QItemSelection) -> None:
        self.selection_changed.emit(self.selected_entries())


__all__ = ["FilePane"]
