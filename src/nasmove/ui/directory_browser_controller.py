from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any, cast

from PySide6.QtCore import QObject, QThread, Signal, Slot

from nasmove.core.model import RemotePath
from nasmove.core.ports import RemoteEntry
from nasmove.smb.error_mapping import redacted_error_code
from nasmove.ui.directory_models import (
    DirectoryEntryViewModel,
    DirectorySide,
    DirectorySnapshot,
)
from nasmove.ui.worker import BackgroundCommandWorker


class DirectoryBrowserController(QObject):
    snapshot_ready = Signal(object)
    loading_changed = Signal(object)
    error_code = Signal(object)

    def __init__(self, *, gateway: object | None = None) -> None:
        super().__init__()
        self._gateway = gateway
        self._sequence = 0
        self._current: dict[DirectorySide, int] = {
            DirectorySide.LOCAL: 0,
            DirectorySide.REMOTE: 0,
        }
        self._threads: set[QThread] = set()
        self._workers: set[BackgroundCommandWorker] = set()
        self._remote_lock = Lock()
        self._stopping = False

    @property
    def can_browse_remote(self) -> bool:
        return self._gateway is not None

    def browse_local(self, path: Path) -> None:
        if self._stopping:
            return
        request_id = self._next(DirectorySide.LOCAL)
        self._start(
            DirectorySide.LOCAL,
            request_id,
            lambda: self._read_local(path, request_id),
        )

    def browse_remote(self, path: RemotePath | None, profile_id: str) -> None:
        if self._stopping:
            return
        request_id = self._next(DirectorySide.REMOTE)
        self._start(
            DirectorySide.REMOTE,
            request_id,
            lambda: self._read_remote(path, profile_id, request_id),
        )

    def make_remote_dir(
        self, path: RemotePath, profile_id: str, parent_location: str = ""
    ) -> None:
        if self._stopping or self._gateway is None:
            return
        gateway = cast(Any, self._gateway)
        if not hasattr(gateway, "make_dir"):
            return
        request_id = self._next(DirectorySide.REMOTE)
        parent_path = RemotePath(parent_location) if parent_location else None

        def _action() -> DirectorySnapshot:
            with self._remote_lock:
                gateway.make_dir(path)
            return self._read_remote(parent_path, profile_id, request_id)

        self._start(DirectorySide.REMOTE, request_id, _action)

    def invalidate(self, side: DirectorySide) -> None:
        """Make any in-flight result for one pane stale without blocking the UI."""
        self._next(side)
        self.loading_changed.emit((side, False))

    def shutdown(self, *, timeout_ms: int = 2_000) -> bool:
        """Stop accepting directory reads and wait for their QThreads to exit."""
        self._stopping = True
        for side in DirectorySide:
            self.invalidate(side)
        threads = tuple(self._threads)
        for thread in threads:
            thread.requestInterruption()
            thread.quit()
        for thread in threads:
            if thread.isRunning():
                thread.wait(timeout_ms)
        return not any(thread.isRunning() for thread in threads)

    def _next(self, side: DirectorySide) -> int:
        self._sequence += 1
        self._current[side] = self._sequence
        return self._sequence

    def _start(
        self,
        side: DirectorySide,
        request_id: int,
        command: Callable[[], DirectorySnapshot],
    ) -> None:
        self.loading_changed.emit((side, True))
        worker = BackgroundCommandWorker(command)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._publish_if_current)
        worker.failed.connect(
            lambda message, requested_side=side, rid=request_id: self._show_error(
                requested_side, rid, message
            )
        )
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda finished_thread=thread, finished_worker=worker: self._finished(
                finished_thread, finished_worker, side, request_id
            )
        )
        self._threads.add(thread)
        self._workers.add(worker)
        thread.start()

    @Slot(object)
    def _publish_if_current(self, snapshot: object) -> None:
        if not isinstance(snapshot, DirectorySnapshot):
            return
        if self._current[snapshot.side] == snapshot.request_id:
            self.snapshot_ready.emit(snapshot)

    def _show_error(
        self, side: DirectorySide, request_id: int, message: str
    ) -> None:
        if self._current[side] == request_id:
            self.error_code.emit((side, redacted_error_code(RuntimeError(message))))

    def _finished(
        self,
        thread: QThread,
        worker: BackgroundCommandWorker,
        side: DirectorySide,
        request_id: int,
    ) -> None:
        self._threads.discard(thread)
        self._workers.discard(worker)
        if self._current[side] == request_id:
            self.loading_changed.emit((side, False))

    @staticmethod
    def _read_local(path: Path, request_id: int) -> DirectorySnapshot:
        entries: list[DirectoryEntryViewModel] = []
        current_thread = QThread.currentThread()
        with os.scandir(path) as listing:
            for entry in listing:
                if current_thread is not None and current_thread.isInterruptionRequested():
                    break
                info = entry.stat(follow_symlinks=False)
                entries.append(
                    DirectoryEntryViewModel(
                        name=entry.name,
                        is_directory=entry.is_dir(follow_symlinks=False),
                        size=info.st_size,
                        modified_ns=info.st_mtime_ns,
                        identity=str(Path(entry.path).absolute()),
                    )
                )
        return DirectorySnapshot(
            DirectorySide.LOCAL, str(path), tuple(entries), request_id, None
        )

    def _read_remote(
        self,
        path: RemotePath | None,
        profile_id: str,
        request_id: int,
    ) -> DirectorySnapshot:
        if self._gateway is None:
            raise RuntimeError("remote gateway is unavailable")
        gateway = cast(Any, self._gateway)
        with self._remote_lock:
            reader = gateway.list_share_root if path is None else gateway.list_dir
            remote_entries = cast(list[RemoteEntry], reader() if path is None else reader(path))
        location = "" if path is None else path.value
        current_thread = QThread.currentThread()
        entries_list: list[DirectoryEntryViewModel] = []
        for entry in remote_entries:
            if current_thread is not None and current_thread.isInterruptionRequested():
                break
            entries_list.append(
                DirectoryEntryViewModel(
                    name=entry.name,
                    is_directory=entry.is_directory,
                    size=entry.size,
                    modified_ns=None,
                    identity=f"{location}/{entry.name}" if location else entry.name,
                )
            )
        return DirectorySnapshot(
            DirectorySide.REMOTE, location, tuple(entries_list), request_id, profile_id
        )


__all__ = ["DirectoryBrowserController"]
