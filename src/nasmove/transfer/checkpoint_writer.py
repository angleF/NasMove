from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Protocol

from nasmove.core.model import Checkpoint, RemotePath, TransferItemRecord
from nasmove.core.ports import RemoteStat, SessionInfo
from nasmove.core.states import SourceKind
from nasmove.smb.smbprotocol_gateway import write_all

IO_BLOCK_BYTES = 4 * 1024 * 1024
CHECKPOINT_BYTES = 64 * 1024 * 1024


class CopyOutcome(StrEnum):
    COMPLETED = "completed"
    PAUSED = "paused"
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
    """Cooperative pause token checked only after each completed I/O block."""

    def __init__(self) -> None:
        self._pause_requested = False

    @property
    def pause_requested(self) -> bool:
        return self._pause_requested

    def request_pause(self) -> None:
        self._pause_requested = True


class TaskRepository(Protocol):
    def save_checkpoint(self, checkpoint: Checkpoint) -> None: ...


class LocalFileGateway(Protocol):
    def open_read(self, path: Path) -> AbstractContextManager[BinaryIO]: ...


class SmbGateway(Protocol):
    def stat(self, path: RemotePath) -> RemoteStat | None: ...

    def open_update(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...

    def create_exclusive(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...


def _pause_requested(token: object | None) -> bool:
    if token is None:
        return False
    value = getattr(token, "pause_requested", None)
    if value is None:
        value = getattr(token, "is_pause_requested", False)
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
    ) -> None:
        if cancellation_token is not None and token is not None:
            raise ValueError("provide only one cancellation token")
        self._repository = repository
        self._local = local_gateway
        self._smb = smb_gateway
        self._token = cancellation_token if cancellation_token is not None else token

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
        active_token = token if token is not None else self._token
        pending_window = b""
        try:
            with self._local.open_read(item.source_path) as source:
                source.seek(start_offset)
                with self._open_remote(item, start_offset) as remote:
                    while True:
                        block = source.read(IO_BLOCK_BYTES)
                        if not isinstance(block, bytes):
                            raise TypeError("local source read must return bytes")
                        if not block:
                            if offset != item.source_fingerprint.size:
                                raise OSError("local source ended before its planned size")
                            if offset != last_persisted or last_persisted == start_offset:
                                self._persist_checkpoint(
                                    item=item,
                                    offset=offset,
                                    window=pending_window,
                                    session=session,
                                    remote=remote,
                                )
                                last_persisted = offset
                            return CopyResult(CopyOutcome.COMPLETED, offset, last_persisted)

                        write_all(remote, block)
                        offset += len(block)
                        pending_window = block[-IO_BLOCK_BYTES:]
                        pause = _pause_requested(active_token)
                        if offset - last_persisted >= CHECKPOINT_BYTES or pause:
                            self._persist_checkpoint(
                                item=item,
                                offset=offset,
                                window=pending_window,
                                session=session,
                                remote=remote,
                            )
                            last_persisted = offset
                            if pause:
                                return CopyResult(CopyOutcome.PAUSED, offset, last_persisted)
        except Exception as error:  # noqa: BLE001 - I/O boundaries must be resumable
            return CopyResult(CopyOutcome.INTERRUPTED, last_persisted, last_persisted, error)

    @staticmethod
    def _validate_request(item: TransferItemRecord, start_offset: int, session: SessionInfo) -> None:
        if item.source_fingerprint.kind is not SourceKind.FILE:
            raise ValueError("checkpoint writer accepts regular files only")
        if type(start_offset) is not int or start_offset < 0:
            raise ValueError("start_offset must be a non-negative integer")
        if start_offset > item.source_fingerprint.size:
            raise ValueError("start_offset must not exceed source size")
        if type(session.session_generation) is not int or session.session_generation <= 0:
            raise ValueError("session generation must be positive")

    @contextmanager
    def _open_remote(self, item: TransferItemRecord, start_offset: int) -> Iterator[BinaryIO]:
        if start_offset == 0:
            with self._smb.create_exclusive(item.temp_path) as stream:
                yield stream
            return
        current = self._smb.stat(item.temp_path)
        if current is None or current.is_directory or current.size != start_offset:
            raise OSError("remote temporary file does not match the durable offset")
        with self._smb.open_update(item.temp_path) as stream:
            stream.seek(start_offset)
            yield stream

    def _persist_checkpoint(
        self,
        *,
        item: TransferItemRecord,
        offset: int,
        window: bytes,
        session: SessionInfo,
        remote: BinaryIO,
    ) -> None:
        digest = hashlib.sha256(window).hexdigest()
        remote.flush()
        remote_stat = self._smb.stat(item.temp_path)
        if remote_stat is None or remote_stat.is_directory or remote_stat.size != offset:
            raise OSError("remote flush length could not be confirmed")
        self._repository.save_checkpoint(
            Checkpoint(
                item_id=item.id,
                confirmed_offset=offset,
                remote_size=remote_stat.size,
                window_start=offset - len(window),
                window_length=len(window),
                window_sha256=digest,
                session_generation=session.session_generation,
            )
        )
__all__ = [
    "CHECKPOINT_BYTES",
    "IO_BLOCK_BYTES",
    "CancellationToken",
    "CheckpointWriter",
    "CopyOutcome",
    "CopyResult",
]
