from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from nasmove.core.model import Checkpoint, RemotePath, TransferItemRecord
from nasmove.core.ports import RemoteStat, SessionInfo
from nasmove.core.states import SourceKind
from nasmove.localio.hashing import sha256_range
from nasmove.smb.smbprotocol_gateway import write_all

IO_BLOCK_BYTES = 4 * 1024 * 1024
CHECKPOINT_BYTES = 64 * 1024 * 1024


class CopyOutcome(StrEnum):
    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class CopyResult:
    outcome: CopyOutcome
    bytes_copied: int
    confirmed_offset: int
    error: Exception | None = None

    @property
    def last_persisted_offset(self) -> int:
        return self.confirmed_offset


class CancellationToken:
    """Cooperative pause/cancel token checked only at block boundaries."""

    def __init__(self) -> None:
        self._pause_requested = False
        self._cancel_requested = False

    @property
    def pause_requested(self) -> bool:
        return self._pause_requested

    def request_pause(self) -> None:
        self._pause_requested = True

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def request_cancel(self) -> None:
        self._cancel_requested = True


class TaskRepository(Protocol):
    def save_checkpoint(self, checkpoint: Checkpoint) -> None: ...


class LocalFileGateway(Protocol):
    def open_read(self, path: Path) -> AbstractContextManager[BinaryIO]: ...


class SmbGateway(Protocol):
    def is_generation_current(self, generation: int) -> bool: ...

    def session_lease(self, generation: int) -> AbstractContextManager[None]: ...

    def stat(self, path: RemotePath) -> RemoteStat | None: ...

    def open_update(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...

    def create_exclusive(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...

    def make_dir(self, path: RemotePath) -> None: ...


def _pause_requested(token: object | None) -> bool:
    if token is None:
        return False
    value = getattr(token, "pause_requested", None)
    if value is None:
        value = getattr(token, "is_pause_requested", False)
    if callable(value):
        value = value()
    return type(value) is bool and value


def _cancel_requested(token: object | None) -> bool:
    if token is None:
        return False
    value = getattr(token, "cancel_requested", None)
    if value is None:
        value = getattr(token, "is_cancel_requested", False)
    if callable(value):
        value = value()
    return type(value) is bool and value


class CheckpointWriter:
    """Stream a source file to a remote temporary file with durable checkpoints.

    A checkpoint is deliberately committed only after the remote stream has
    flushed and a subsequent remote stat confirms the exact length.  Bytes
    written after the last such commit are therefore never reported as
    recoverable progress.
    """

    def __init__(
        self,
        repository: TaskRepository,
        local_gateway: LocalFileGateway,
        smb_gateway: SmbGateway,
        cancellation_token: CancellationToken | None = None,
        token: CancellationToken | None = None,
        progress: Callable[[TransferItemRecord, int], None] | None = None,
    ) -> None:
        if cancellation_token is not None and token is not None:
            raise ValueError("provide only one cancellation token")
        self._repository = repository
        self._local = local_gateway
        self._smb = smb_gateway
        self._token = cancellation_token if cancellation_token is not None else token
        self._progress = progress

    def copy(
        self,
        item: TransferItemRecord,
        start_offset: int,
        session: SessionInfo,
        token: CancellationToken | None = None,
    ) -> CopyResult:
        self._validate_request(item, start_offset, session)
        last_persisted = start_offset
        offset = start_offset
        if self._progress is not None:
            self._progress(item, start_offset)
        active_token = token if token is not None else self._token
        if _cancel_requested(active_token):
            return CopyResult(CopyOutcome.CANCELLED, start_offset, last_persisted)
        try:
            self._ensure_parent_directories(item.temp_path, session)
            if item.source_fingerprint.kind is SourceKind.EMPTY_DIRECTORY:
                self._prepare_empty_directory(item, session)
                return CopyResult(CopyOutcome.COMPLETED, 0, 0)
            with self._local.open_read(item.source_path) as source:
                source.seek(start_offset)
                with self._open_remote(item, start_offset, session) as remote:
                    while True:
                        block = source.read(IO_BLOCK_BYTES)
                        if not isinstance(block, bytes):
                            raise TypeError("local source read must return bytes")
                        if not block:
                            if offset != item.source_fingerprint.size:
                                raise OSError("local source ended before its planned size")
                            if offset != last_persisted or last_persisted == start_offset:
                                persisted = self._persist_checkpoint(
                                    item=item,
                                    offset=offset,
                                    source=source,
                                    session=session,
                                    remote=remote,
                                    token=active_token,
                                )
                                if not persisted:
                                    return CopyResult(
                                        CopyOutcome.CANCELLED, last_persisted, last_persisted
                                    )
                                last_persisted = offset
                            return CopyResult(CopyOutcome.COMPLETED, offset, last_persisted)

                        write_all(remote, block)
                        offset += len(block)
                        if _cancel_requested(active_token):
                            return CopyResult(CopyOutcome.CANCELLED, last_persisted, last_persisted)
                        pause = _pause_requested(active_token)
                        if offset - last_persisted >= CHECKPOINT_BYTES or pause:
                            persisted = self._persist_checkpoint(
                                item=item,
                                offset=offset,
                                source=source,
                                session=session,
                                remote=remote,
                                token=active_token,
                            )
                            if not persisted:
                                return CopyResult(
                                    CopyOutcome.CANCELLED, last_persisted, last_persisted
                                )
                            last_persisted = offset
                            if pause:
                                return CopyResult(CopyOutcome.PAUSED, offset, last_persisted)
        except Exception as error:  # noqa: BLE001 - I/O boundaries must be resumable
            return CopyResult(CopyOutcome.INTERRUPTED, last_persisted, last_persisted, error)

    @staticmethod
    def _validate_request(item: TransferItemRecord, start_offset: int, session: SessionInfo) -> None:
        if item.source_fingerprint.kind not in {SourceKind.FILE, SourceKind.EMPTY_DIRECTORY}:
            raise ValueError("checkpoint writer accepts regular files and empty directories only")
        if type(start_offset) is not int or start_offset < 0:
            raise ValueError("start_offset must be a non-negative integer")
        if start_offset > item.source_fingerprint.size:
            raise ValueError("start_offset must not exceed source size")
        if start_offset > item.confirmed_offset:
            raise ValueError("start_offset must not exceed durable item checkpoint")
        if type(session.session_generation) is not int or session.session_generation <= 0:
            raise ValueError("session generation must be positive")

    def _ensure_parent_directories(self, path: RemotePath, session: SessionInfo) -> None:
        parent = PurePosixPath(path.value).parent
        if str(parent) == ".":
            return
        current_parts: list[str] = []
        with self._smb.session_lease(session.session_generation):
            for component in parent.parts:
                current_parts.append(component)
                current = RemotePath("/".join(current_parts))
                self._assert_generation(session)
                remote_stat = self._smb.stat(current)
                if remote_stat is None:
                    try:
                        self._smb.make_dir(current)
                    except Exception:
                        remote_stat = self._smb.stat(current)
                        if remote_stat is None:
                            raise
                    else:
                        remote_stat = self._smb.stat(current)
                if remote_stat is None or not remote_stat.is_directory:
                    raise OSError(f"remote parent is not a directory: {current.value}")
            self._assert_generation(session)

    def _prepare_empty_directory(
        self, item: TransferItemRecord, session: SessionInfo
    ) -> None:
        with self._smb.session_lease(session.session_generation):
            self._assert_generation(session)
            remote_stat = self._smb.stat(item.temp_path)
            if remote_stat is None:
                try:
                    self._smb.make_dir(item.temp_path)
                except Exception:
                    remote_stat = self._smb.stat(item.temp_path)
                    if remote_stat is None:
                        raise
                else:
                    remote_stat = self._smb.stat(item.temp_path)
            self._assert_generation(session)
            if remote_stat is None or not remote_stat.is_directory:
                raise OSError("remote temporary directory could not be confirmed")

    @contextmanager
    def _open_remote(
        self,
        item: TransferItemRecord,
        start_offset: int,
        session: SessionInfo,
    ) -> Iterator[BinaryIO]:
        self._assert_generation(session)
        current = self._smb.stat(item.temp_path)
        self._assert_generation(session)
        if start_offset == 0:
            if current is not None:
                raise OSError("remote temporary file already exists for a new copy")
            with self._smb.create_exclusive(item.temp_path) as stream:
                yield stream
            return
        if current is None or current.is_directory or current.size != start_offset:
            raise OSError("remote temporary file does not match the durable offset")
        self._assert_generation(session)
        with self._smb.open_update(item.temp_path) as stream:
            stream.seek(start_offset)
            yield stream

    def _persist_checkpoint(
        self,
        *,
        item: TransferItemRecord,
        offset: int,
        source: BinaryIO,
        session: SessionInfo,
        remote: BinaryIO,
        token: CancellationToken | None,
    ) -> bool:
        if _cancel_requested(token):
            return False
        window_length = min(IO_BLOCK_BYTES, offset)
        window_start = offset - window_length
        digest = sha256_range(source, window_start, window_length)
        source.seek(offset)
        if _cancel_requested(token):
            return False
        with self._smb.session_lease(session.session_generation):
            self._assert_generation(session)
            remote.flush()
            self._assert_generation(session)
            remote_stat = self._smb.stat(item.temp_path)
            self._assert_generation(session)
            if remote_stat is None or remote_stat.is_directory or remote_stat.size != offset:
                raise OSError("remote flush length could not be confirmed")
            self._repository.save_checkpoint(
                Checkpoint(
                    item_id=item.id,
                    confirmed_offset=offset,
                    remote_size=remote_stat.size,
                    window_start=window_start,
                    window_length=window_length,
                    window_sha256=digest,
                    session_generation=session.session_generation,
                )
            )
        if self._progress is not None:
            self._progress(item, offset)
        return True

    def _assert_generation(self, session: SessionInfo) -> None:
        checker = getattr(self._smb, "is_generation_current", None)
        if not callable(checker) or checker(session.session_generation) is not True:
            raise OSError("SMB session generation changed during checkpoint")
__all__ = [
    "CHECKPOINT_BYTES",
    "IO_BLOCK_BYTES",
    "CancellationToken",
    "CheckpointWriter",
    "CopyOutcome",
    "CopyResult",
]
