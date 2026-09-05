from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from io import BytesIO
from typing import BinaryIO

from smbprotocol.exceptions import SMBOSError
from smbprotocol.header import NtStatus

from nasmove.core.model import ConnectionConfig, RemotePath
from nasmove.core.ports import RemoteEntry, RemoteStat, SessionInfo
from nasmove.smb.error_mapping import RenameOutcomeUnknownError


class _RecordingStream(BytesIO):
    def __init__(self, gateway: RecordingSmbGateway, path: RemotePath, *, update: bool) -> None:
        super().__init__(gateway.files.get(path.value, b""))
        self._gateway = gateway
        self._path = path
        self._update = update

    def write(self, data: bytes | bytearray) -> int:
        if not self._update and "write_initial" not in self._gateway.calls:
            self._gateway.calls.append("write_initial")
        return super().write(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        if self._update and offset > 0 and whence == 0:
            self._gateway.calls.append("seek_append")
        return super().seek(offset, whence)

    def flush(self) -> None:
        if not self.closed:
            self._gateway.files[self._path.value] = self.getvalue()
            if not self._update:
                self._gateway.calls.append("flush")
        super().flush()

    def close(self) -> None:
        if not self.closed:
            self._gateway.files[self._path.value] = self.getvalue()
        super().close()


class RecordingSmbGateway:
    def __init__(
        self,
        *,
        fail_cleanup: bool = False,
        corrupt_read: bool = False,
        create_error: Exception | None = None,
        ambiguous_rename: bool = False,
        real_missing_remove: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.files: dict[str, bytes] = {}
        self.created_paths: list[RemotePath] = []
        self.max_file_size = 0
        self._generation = 0
        self._fail_cleanup = fail_cleanup
        self._corrupt_read = corrupt_read
        self._create_error = create_error
        self._ambiguous_rename = ambiguous_rename
        self._real_missing_remove = real_missing_remove

    def connect(self, config: ConnectionConfig, password: str) -> SessionInfo:
        del config, password
        self._generation += 1
        return SessionInfo("3.1.1", True, True, self._generation)

    def disconnect(self) -> None:
        return None

    def reset_connection(self) -> None:
        return None

    def stat(self, path: RemotePath) -> RemoteStat | None:
        value = self.files.get(path.value)
        if value is None:
            return None
        return RemoteStat(size=len(value), is_directory=False, modified_ns=0, file_id=path.value)

    def list_dir(self, path: RemotePath) -> list[RemoteEntry]:
        prefix = f"{path.value}/"
        return [
            RemoteEntry(name=name.removeprefix(prefix), is_directory=False, size=len(value))
            for name, value in self.files.items()
            if name.startswith(prefix)
        ]

    @contextmanager
    def open_read(self, path: RemotePath) -> Iterator[BinaryIO]:
        self.calls.append("open_read")
        value = self.files[path.value]
        if self._corrupt_read:
            value += b"corrupt"
        yield BytesIO(value)

    @contextmanager
    def open_update(self, path: RemotePath) -> Iterator[BinaryIO]:
        self.calls.append("reopen_update")
        stream = _RecordingStream(self, path, update=True)
        try:
            yield stream
        finally:
            stream.close()

    @contextmanager
    def create_exclusive(self, path: RemotePath) -> Iterator[BinaryIO]:
        self.calls.append("create_exclusive")
        if self._create_error is not None:
            raise self._create_error
        if path.value in self.files:
            raise FileExistsError(path.value)
        self.files[path.value] = b""
        self.created_paths.append(path)
        stream = _RecordingStream(self, path, update=False)
        try:
            yield stream
        finally:
            stream.close()
            self.max_file_size = max(self.max_file_size, len(self.files[path.value]))

    def truncate(self, path: RemotePath, size: int) -> None:
        self.calls.append("truncate")
        self.files[path.value] = self.files[path.value][:size]

    def rename_exclusive(self, source: RemotePath, target: RemotePath) -> None:
        self.calls.append("rename_exclusive")
        if target.value in self.files:
            raise FileExistsError(target.value)
        self.files[target.value] = self.files.pop(source.value)
        if self._ambiguous_rename:
            raise RenameOutcomeUnknownError("simulated lost rename response")

    def remove_file(self, path: RemotePath) -> None:
        self.calls.append("remove_file")
        if self._fail_cleanup:
            raise PermissionError("simulated cleanup denial")
        if path.value not in self.files:
            if self._real_missing_remove:
                raise SMBOSError(NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, path.value)
            raise FileNotFoundError(path.value)
        del self.files[path.value]

    def make_dir(self, path: RemotePath) -> None:
        del path

    def free_space(self, path: RemotePath) -> int:
        del path
        return 1 << 40


class ShortWritingStream(BytesIO):
    def __init__(self, max_bytes_per_call: int) -> None:
        super().__init__()
        self.max_bytes_per_call = max_bytes_per_call
        self.write_calls = 0

    @property
    def value(self) -> bytes:
        return self.getvalue()

    def write(self, data: bytes | bytearray) -> int:
        self.write_calls += 1
        return super().write(data[: self.max_bytes_per_call])


class FakeNtStatusError(Exception):
    def __init__(self, status: str | int) -> None:
        super().__init__(status)
        self.status = status


def binary_context(value: bytes = b"") -> AbstractContextManager[BinaryIO]:
    @contextmanager
    def open_stream() -> Iterator[BinaryIO]:
        yield BytesIO(value)

    return open_stream()
