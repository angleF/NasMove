from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import BinaryIO

import pytest

from nasmove.core.model import (
    Checkpoint,
    RemotePath,
    SourceFingerprint,
    TaskId,
    TransferItemId,
    TransferItemRecord,
)
from nasmove.core.ports import RemoteStat, SessionInfo
from nasmove.core.states import ItemState, SourceKind


class TransferLocal:
    def __init__(self, content: bytes, trace: list[str]) -> None:
        self.content = content
        self.trace = trace
        self.cancel_token: TransferToken | None = None
        self.cancel_on_eof = False

    @contextmanager
    def open_read(self, path: Path) -> Iterator[BinaryIO]:
        del path
        yield _TracingLocalStream(self.content, self.trace, self.cancel_token, self.cancel_on_eof)


class _TracingLocalStream(BytesIO):
    def __init__(
        self,
        content: bytes,
        trace: list[str],
        cancel_token: TransferToken | None,
        cancel_on_eof: bool,
    ) -> None:
        super().__init__(content)
        self._trace = trace
        self._cancel_token = cancel_token
        self._cancel_on_eof = cancel_on_eof
        self._read_count = 0
        self._hashing = False

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 2:
            self._hashing = True
        return super().seek(offset, whence)

    def read(self, size: int = -1) -> bytes:
        result = super().read(size)
        self._read_count += 1
        if result and self._cancel_token is not None and self._read_count == 1:
            self._cancel_token.cancel_requested = True
        if not result and self._cancel_token is not None and self._cancel_on_eof:
            self._cancel_token.cancel_requested = True
        if self._hashing and result:
            self._trace.append("local.hash")
            self._hashing = False
        return result


class TransferRemote:
    def __init__(self, trace: list[str], *, content: bytes = b"") -> None:
        self.trace = trace
        self.files: dict[str, bytearray] = {}
        self.content = content
        self.path: RemotePath | None = None
        self.fail_write = False
        self.fail_flush = False
        self.fail_flush_after_write = False
        self.fail_stat = False
        self.short_write = False
        self.active_generation = 1
        self._lifecycle_lock = RLock()
        self._lease_owner: int | None = None
        self.generation_switch_blocked = False
        self.pending: dict[str, bytearray] = {}
        self.switch_generation_after_flush = False
        self.switch_generation_after_stat = False
        self.fail_stat_after_flush = False

    @contextmanager
    def create_exclusive(self, path: RemotePath) -> Iterator[BinaryIO]:
        self.trace.append(f"remote.create@{path.value}")
        if path.value in self.files or path.value in self.pending:
            raise FileExistsError(path.value)
        self.path = path
        self.files[path.value] = bytearray()
        self.pending[path.value] = bytearray()
        stream = _TracingRemoteStream(self, path)
        try:
            yield stream
        finally:
            stream.close()

    @contextmanager
    def open_update(self, path: RemotePath) -> Iterator[BinaryIO]:
        self.trace.append(f"remote.open_update@{path.value}")
        self.path = path
        if path.value not in self.files:
            raise FileNotFoundError(path.value)
        stream = _TracingRemoteStream(self, path)
        try:
            yield stream
        finally:
            stream.close()

    def stat(self, path: RemotePath) -> RemoteStat | None:
        if self.fail_stat:
            raise OSError("stat failure")
        value = self.files.get(path.value)
        if value is None:
            return None
        if self.fail_stat_after_flush and value:
            raise OSError("stat failure after flush")
        self.trace.append(f"remote.stat@{len(value)}")
        result = RemoteStat(len(value), False, 0, path.value)
        if self.switch_generation_after_stat:
            self.active_generation += 1
            self.switch_generation_after_stat = False
        return result

    def is_generation_current(self, generation: int) -> bool:
        with self._lifecycle_lock:
            return self.active_generation == generation

    @contextmanager
    def session_lease(self, generation: int) -> Iterator[None]:
        with self._lifecycle_lock:
            if not self.is_generation_current(generation):
                raise OSError("stale session")
            self._lease_owner = 1
            try:
                yield
            finally:
                self._lease_owner = None

    def request_generation_switch(self) -> bool:
        acquired = self._lifecycle_lock.acquire(blocking=False)
        if not acquired:
            self.generation_switch_blocked = True
            return False
        try:
            if self._lease_owner is not None:
                self.generation_switch_blocked = True
                return False
            self.active_generation += 1
            return True
        finally:
            self._lifecycle_lock.release()

    def flush(self, stream: BinaryIO, offset: int) -> None:
        del stream
        self.trace.append(f"remote.flush@{offset}")


class _TracingRemoteStream(BytesIO):
    def __init__(self, remote: TransferRemote, path: RemotePath) -> None:
        self._remote = remote
        self._path = path
        super().__init__(bytes(remote.files.get(path.value, b"")))

    def write(self, data: bytes | bytearray) -> int:
        if self._remote.fail_write:
            raise OSError("write failure")
        payload = data[: 123 if self._remote.short_write else len(data)]
        written = super().write(payload)
        self._remote.pending[self._path.value] = bytearray(self.getvalue())
        return written

    def seek(self, offset: int, whence: int = 0) -> int:
        return super().seek(offset, whence)

    def flush(self) -> None:
        if self._remote.fail_flush:
            raise OSError("flush failure")
        self._remote.files[self._path.value] = bytearray(self.getvalue())
        self._remote.pending[self._path.value] = bytearray(self.getvalue())
        self._remote.trace.append(f"remote.flush@{len(self.getvalue())}")
        super().flush()
        if self._remote.switch_generation_after_flush:
            self._remote.active_generation += 1
            self._remote.switch_generation_after_flush = False
        if self._remote.fail_flush_after_write:
            raise OSError("flush result unknown")


class TransferRepository:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.checkpoints: list[Checkpoint] = []

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        offset = checkpoint.confirmed_offset
        self.trace.append(f"repository.save_checkpoint@{offset}")
        self.checkpoints.append(checkpoint)


class TransferToken:
    def __init__(self, pause: bool = False) -> None:
        self.pause_requested = pause
        self.cancel_requested = False


@dataclass
class FakeDependencies:
    local: TransferLocal
    remote: TransferRemote
    repository: TransferRepository
    trace: list[str]
    cancellation_token: TransferToken

    def as_kwargs(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "local_gateway": self.local,
            "smb_gateway": self.remote,
            "cancellation_token": self.cancellation_token,
        }

    def item(
        self,
        *,
        size: int | None = None,
        confirmed_offset: int = 0,
    ) -> TransferItemRecord:
        if size is None:
            size = len(self.local.content)
        content = self.local.content[:size]
        self.local.content = content
        return TransferItemRecord(
            id=TransferItemId("item-1"),
            task_id=TaskId("task-1"),
            source_path=Path("/source/file.bin"),
            relative_path=PurePosixPath("file.bin"),
            final_path=RemotePath("target/file.bin"),
            temp_path=RemotePath("target/.file.bin.part"),
            source_fingerprint=SourceFingerprint(1, 2, SourceKind.FILE, size, 1),
            state=ItemState.TRANSFERRING,
            confirmed_offset=confirmed_offset,
        )

    def session(self, *, generation: int) -> SessionInfo:
        self.remote.active_generation = generation
        return SessionInfo("3.1.1", True, True, generation)


@pytest.fixture
def fake_dependencies() -> FakeDependencies:
    trace: list[str] = []
    return FakeDependencies(
        local=TransferLocal(bytes(range(256)) * (70 * 1024 * 1024 // 256 + 1), trace),
        remote=TransferRemote(trace),
        repository=TransferRepository(trace),
        trace=trace,
        cancellation_token=TransferToken(),
    )
