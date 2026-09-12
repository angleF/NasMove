from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt

_INVALID_INDEX = QModelIndex()


class DirectorySide(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


@dataclass(frozen=True, slots=True)
class DirectoryEntryViewModel:
    name: str
    is_directory: bool
    size: int
    modified_ns: int | None
    identity: str


@dataclass(frozen=True, slots=True)
class DirectorySnapshot:
    side: DirectorySide
    location: str
    entries: tuple[DirectoryEntryViewModel, ...]
    request_id: int
    profile_id: str | None


def _size_text(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{size} B"


def _modified_text(modified_ns: int) -> str:
    try:
        modified_at = datetime.fromtimestamp(
            modified_ns / 1_000_000_000, tz=UTC
        ).astimezone()
    except (OSError, OverflowError, ValueError):
        return "—"
    today = datetime.now(tz=UTC).astimezone().date()
    if modified_at.date() == today:
        return "今天"
    if modified_at.date() == today - timedelta(days=1):
        return "昨天"
    return f"{modified_at.month}月{modified_at.day}日"


class DirectoryTableModel(QAbstractTableModel):
    HEADERS = ("名称", "大小", "修改")

    def __init__(self, side: DirectorySide) -> None:
        super().__init__()
        self.side = side
        self._snapshot = DirectorySnapshot(side, "", (), 0, None)
        self._entries: tuple[DirectoryEntryViewModel, ...] = ()
        self._filter = ""
        self._sort_column = 0
        self._sort_order = Qt.SortOrder.AscendingOrder

    @property
    def snapshot(self) -> DirectorySnapshot:
        return self._snapshot

    def replace(self, snapshot: DirectorySnapshot) -> None:
        self.beginResetModel()
        self._snapshot = snapshot
        self._apply_filter()
        self.endResetModel()

    def set_filter(self, text: str) -> None:
        self.beginResetModel()
        self._filter = text.strip().casefold()
        self._apply_filter()
        self.endResetModel()

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        self.beginResetModel()
        self._sort_column = column
        self._sort_order = order
        self._apply_filter()
        self.endResetModel()

    def entry_at(self, row: int) -> DirectoryEntryViewModel:
        return self._entries[row]

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        entry = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return entry.name
            if index.column() == 1:
                return "—" if entry.is_directory else _size_text(entry.size)
            if index.column() == 2:
                return "—" if entry.modified_ns is None else _modified_text(entry.modified_ns)
        if role == Qt.ItemDataRole.UserRole:
            return entry
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.name
        if role == Qt.ItemDataRole.AccessibleTextRole:
            kind = "文件夹" if entry.is_directory else "文件"
            return f"{kind} {entry.name}"
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def _apply_filter(self) -> None:
        entries = self._snapshot.entries
        if self._filter:
            entries = tuple(entry for entry in entries if self._filter in entry.name.casefold())
        reverse = self._sort_order == Qt.SortOrder.DescendingOrder

        def sort_key(entry: DirectoryEntryViewModel):
            if self._sort_column == 1:
                return (entry.size, entry.name.casefold())
            if self._sort_column == 2:
                return (entry.modified_ns or 0, entry.name.casefold())
            return (entry.name.casefold(),)

        folders = [e for e in entries if e.is_directory]
        files = [e for e in entries if not e.is_directory]
        folders.sort(key=sort_key, reverse=reverse)
        files.sort(key=sort_key, reverse=reverse)
        self._entries = tuple(folders + files)


__all__ = [
    "DirectoryEntryViewModel",
    "DirectorySide",
    "DirectorySnapshot",
    "DirectoryTableModel",
]
