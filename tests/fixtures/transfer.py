from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
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
    def __init__(self, content: bytes) -> None:
        self.content = content

    @contextmanager
    def open_read(self, path: Path) -> Iterator[BinaryIO]:
        del path
        yield BytesIO(self.content)


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

    @contextmanager
    def create_exclusive(self, path: RemotePath) -> Iterator[BinaryIO]:
        self.trace.append(f"remote.create@{path.value}")
        self.path = path
        self.files[path.value] = bytearray()
        stream = _TracingRemoteStream(self, path)
        try:
            yield stream
        finally:
            stream.close()

    @contextmanager
    def open_update(self, path: RemotePath) -> Iterator[BinaryIO]:
        self.trace.append(f"remote.open_update@{path.value}")
        self.path = path
        self.files.setdefault(path.value, bytearray())
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
        self.trace.append(f"remote.stat@{len(value)}")
        return RemoteStat(len(value), False, 0, path.value)

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
        self._remote.files[self._path.value] = bytearray(self.getvalue())
        return written

    def seek(self, offset: int, whence: int = 0) -> int:
        return super().seek(offset, whence)

    def flush(self) -> None:
        if self._remote.fail_flush:
            raise OSError("flush failure")
        self._remote.files[self._path.value] = bytearray(self.getvalue())
        self._remote.trace.append(f"remote.flush@{len(self.getvalue())}")
        super().flush()
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

    def item(self, *, size: int | None = None) -> TransferItemRecord:
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
        )

    def session(self, *, generation: int) -> SessionInfo:
        return SessionInfo("3.1.1", True, True, generation)


@pytest.fixture
def fake_dependencies() -> FakeDependencies:
    trace: list[str] = []
    return FakeDependencies(
        local=TransferLocal(bytes(range(256)) * (70 * 1024 * 1024 // 256 + 1)),
        remote=TransferRemote(trace),
        repository=TransferRepository(trace),
        trace=trace,
        cancellation_token=TransferToken(),
    )
