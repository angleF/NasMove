from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from nasmove.core.model import ConnectionConfig, RemotePath


@dataclass(frozen=True, slots=True)
class SessionInfo:
    dialect: str
    signing: bool
    encryption: bool
    session_generation: int


@dataclass(frozen=True, slots=True)
class RemoteStat:
    size: int
    is_directory: bool
    modified_ns: int
    file_id: str | None


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    name: str
    is_directory: bool
    size: int


class SmbGateway(Protocol):
    def connect(self, config: ConnectionConfig, password: str) -> SessionInfo: ...

    def disconnect(self) -> None: ...

    def reset_connection(self) -> None: ...

    def stat(self, path: RemotePath) -> RemoteStat | None: ...

    def list_dir(self, path: RemotePath) -> list[RemoteEntry]: ...

    def open_read(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...

    def open_update(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...

    def create_exclusive(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...

    def truncate(self, path: RemotePath, size: int) -> None: ...

    def rename_exclusive(self, source: RemotePath, target: RemotePath) -> None: ...

    def remove_file(self, path: RemotePath) -> None: ...

    def make_dir(self, path: RemotePath) -> None: ...

    def free_space(self, path: RemotePath) -> int: ...
