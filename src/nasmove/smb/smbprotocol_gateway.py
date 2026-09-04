from __future__ import annotations

import stat as stat_module
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, BinaryIO, cast

import smbclient  # type: ignore[import-untyped]

from nasmove.core.model import ConnectionConfig, RemotePath
from nasmove.core.ports import RemoteEntry, RemoteStat, SessionInfo
from nasmove.smb.error_mapping import (
    StaleSmbHandleError,
    TargetExistsError,
    UnsupportedSmbDialectError,
)

_DIALECT_NAMES = {
    0x0202: "2.0.2",
    0x0210: "2.1",
    0x0300: "3.0",
    0x0302: "3.0.2",
    0x0311: "3.1.1",
}
_DIALECT_VALUES = {name: value for value, name in _DIALECT_NAMES.items()}


class _GenerationCheckedStream:
    def __init__(self, gateway: SmbProtocolGateway, raw: BinaryIO, generation: int) -> None:
        self._gateway = gateway
        self._raw = raw
        self._generation = generation

    @property
    def closed(self) -> bool:
        return self._raw.closed

    def _check_current(self) -> None:
        if not self._gateway.is_generation_current(self._generation):
            raise StaleSmbHandleError("SMB handle belongs to an invalidated session")

    def read(self, size: int = -1) -> bytes:
        self._check_current()
        return self._raw.read(size)

    def write(self, data: bytes) -> int:
        self._check_current()
        return self._raw.write(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        self._check_current()
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        self._check_current()
        return self._raw.tell()

    def flush(self) -> None:
        self._check_current()
        self._raw.flush()

    def truncate(self, size: int | None = None) -> int:
        self._check_current()
        if size is None:
            return self._raw.truncate()
        return self._raw.truncate(size)


class SmbProtocolGateway:
    def __init__(self) -> None:
        self._connection_cache: dict[str, Any] = {}
        self._config: ConnectionConfig | None = None
        self._session_generation = 0
        self._active_generation: int | None = None

    def connect(self, config: ConnectionConfig, password: str) -> SessionInfo:
        if self._active_generation is not None:
            self.disconnect()
        username = f"{config.domain}\\{config.username}" if config.domain else config.username
        session = smbclient.register_session(
            self._normalize_component(config.host, "host"),
            username=username,
            password=password,
            port=config.port,
            encrypt=config.require_encryption,
            connection_cache=self._connection_cache,
            require_signing=True,
        )
        dialect_value = cast(int | None, session.connection.dialect)
        minimum = _DIALECT_VALUES.get(config.minimum_dialect)
        if minimum is None or dialect_value is None or dialect_value < minimum:
            self._clear_connection_cache()
            raise UnsupportedSmbDialectError("negotiated SMB dialect is below the configured minimum")

        self._session_generation += 1
        self._active_generation = self._session_generation
        self._config = config
        return SessionInfo(
            dialect=_DIALECT_NAMES.get(dialect_value, f"0x{dialect_value:04x}"),
            signing=bool(session.signing_required),
            encryption=bool(session.encrypt_data),
            session_generation=self._session_generation,
        )

    def disconnect(self) -> None:
        self._active_generation = None
        self._config = None
        self._clear_connection_cache()

    def reset_connection(self) -> None:
        self._active_generation = None
        self._config = None
        self._clear_connection_cache()

    def is_generation_current(self, generation: int) -> bool:
        return self._active_generation == generation

    def stat(self, path: RemotePath) -> RemoteStat | None:
        try:
            result = smbclient.stat(self._unc(path), **self._session_kwargs())
        except FileNotFoundError:
            return None
        return RemoteStat(
            size=result.st_size,
            is_directory=stat_module.S_ISDIR(result.st_mode),
            modified_ns=result.st_mtime_ns,
            file_id=str(result.st_ino),
        )

    def list_dir(self, path: RemotePath) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        with smbclient.scandir(self._unc(path), **self._session_kwargs()) as iterator:
            for entry in iterator:
                entry_stat = entry.stat()
                entries.append(
                    RemoteEntry(
                        name=entry.name,
                        is_directory=entry.is_dir(),
                        size=entry_stat.st_size,
                    )
                )
        return entries

    def open_read(self, path: RemotePath) -> AbstractContextManager[BinaryIO]:
        return self._open(path, "rb")

    def open_update(self, path: RemotePath) -> AbstractContextManager[BinaryIO]:
        return self._open(path, "r+b")

    def create_exclusive(self, path: RemotePath) -> AbstractContextManager[BinaryIO]:
        return self._open(path, "x+b")

    def truncate(self, path: RemotePath, size: int) -> None:
        if size < 0:
            raise ValueError("SMB truncate size must not be negative")
        with self.open_update(path) as stream:
            stream.truncate(size)
            stream.flush()

    def rename_exclusive(self, source: RemotePath, target: RemotePath) -> None:
        if self.stat(target) is not None:
            raise TargetExistsError("exclusive SMB rename target already exists")
        try:
            smbclient.rename(
                self._unc(source),
                self._unc(target),
                **self._session_kwargs(),
            )
        except FileExistsError as error:
            raise TargetExistsError("exclusive SMB rename target already exists") from error
        except Exception as error:
            if self.stat(target) is not None:
                raise TargetExistsError("exclusive SMB rename target already exists") from error
            raise

    def remove_file(self, path: RemotePath) -> None:
        smbclient.remove(self._unc(path), **self._session_kwargs())

    def make_dir(self, path: RemotePath) -> None:
        smbclient.mkdir(self._unc(path), **self._session_kwargs())

    def free_space(self, path: RemotePath) -> int:
        volume = smbclient.stat_volume(self._unc(path), **self._session_kwargs())
        return cast(int, volume.caller_available_size)

    @contextmanager
    def _open(self, path: RemotePath, mode: str) -> Iterator[BinaryIO]:
        generation = self._require_generation()
        raw = cast(
            BinaryIO,
            smbclient.open_file(
                self._unc(path),
                mode=mode,
                buffering=0,
                **self._session_kwargs(),
            ),
        )
        stream = _GenerationCheckedStream(self, raw, generation)
        try:
            yield cast(BinaryIO, stream)
        finally:
            try:
                raw.close()
            except OSError:
                if self.is_generation_current(generation):
                    raise

    def _unc(self, path: RemotePath) -> str:
        config = self._require_config()
        host = self._normalize_component(config.host, "host")
        share = self._normalize_component(config.share, "share")
        remote = path.value.replace("/", "\\")
        return f"\\\\{host}\\{share}\\{remote}"

    def _session_kwargs(self) -> dict[str, Any]:
        config = self._require_config()
        return {"port": config.port, "connection_cache": self._connection_cache}

    def _require_config(self) -> ConnectionConfig:
        if self._config is None or self._active_generation is None:
            raise ConnectionError("SMB gateway is not connected")
        return self._config

    def _require_generation(self) -> int:
        self._require_config()
        assert self._active_generation is not None
        return self._active_generation

    def _clear_connection_cache(self) -> None:
        smbclient.reset_connection_cache(
            fail_on_error=False,
            connection_cache=self._connection_cache,
        )

    @staticmethod
    def _normalize_component(value: str, field_name: str) -> str:
        normalized = value.strip().strip("\\/")
        if not normalized or "\\" in normalized or "/" in normalized:
            raise ValueError(f"SMB {field_name} must be a single UNC component")
        return normalized
