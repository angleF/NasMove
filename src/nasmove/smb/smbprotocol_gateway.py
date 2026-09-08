from __future__ import annotations

import errno
import ipaddress
import stat as stat_module
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from threading import RLock, get_ident
from typing import Any, BinaryIO, Literal, cast

import smbclient  # type: ignore[import-untyped]

from nasmove.core.model import ConnectionConfig, RemotePath
from nasmove.core.ports import RemoteEntry, RemoteStat, SessionInfo
from nasmove.planning.paths import normalize_remote_path, validate_remote_component
from nasmove.smb.error_mapping import (
    RenameOutcomeUnknownError,
    StaleSmbHandleError,
    TargetExistsError,
    UnsupportedSmbDialectError,
    redacted_error_code,
)

_DIALECT_NAMES = {
    0x0202: "2.0.2",
    0x0210: "2.1",
    0x0300: "3.0",
    0x0302: "3.0.2",
    0x0311: "3.1.1",
}
_DIALECT_VALUES = {name: value for value, name in _DIALECT_NAMES.items()}


def _raise_stale_handle_on_bad_descriptor(error: OSError) -> None:
    if error.errno == errno.EBADF:
        raise StaleSmbHandleError("SMB transport closed the active handle") from error


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

    def _invoke(self, operation: Any, *args: Any) -> Any:
        self._check_current()
        try:
            return operation(*args)
        except OSError as error:
            _raise_stale_handle_on_bad_descriptor(error)
            raise

    def read(self, size: int = -1) -> bytes:
        return cast(bytes, self._invoke(self._raw.read, size))

    def write(self, data: bytes) -> int:
        return cast(int, self._invoke(self._raw.write, data))

    def seek(self, offset: int, whence: int = 0) -> int:
        return cast(int, self._invoke(self._raw.seek, offset, whence))

    def tell(self) -> int:
        return cast(int, self._invoke(self._raw.tell))

    def flush(self) -> None:
        self._invoke(self._raw.flush)

    def truncate(self, size: int | None = None) -> int:
        if size is None:
            return cast(int, self._invoke(self._raw.truncate))
        return cast(int, self._invoke(self._raw.truncate, size))


def write_all(stream: BinaryIO, data: bytes) -> None:
    """Write all bytes, since SMB writes are allowed to complete partially."""
    offset = 0
    while offset < len(data):
        written = stream.write(data[offset:])
        if type(written) is not int or written <= 0 or written > len(data) - offset:
            raise OSError("SMB write made no progress or returned an invalid count")
        offset += written


class SmbProtocolGateway:
    def __init__(
        self,
        *,
        auth_protocol: Literal["negotiate", "ntlm", "kerberos"] = "negotiate",
    ) -> None:
        if auth_protocol not in {"negotiate", "ntlm", "kerberos"}:
            raise ValueError("unsupported SMB authentication protocol")
        self._connection_cache: dict[str, Any] = {}
        self._auth_protocol = auth_protocol
        self._config: ConnectionConfig | None = None
        self._password: str | None = None
        self._session_generation = 0
        self._active_generation: int | None = None
        self._lifecycle_lock = RLock()
        self._lease_owner: int | None = None

    def connect(self, config: ConnectionConfig, password: str) -> SessionInfo:
        with self._lifecycle_lock:
            self._ensure_lifecycle_available()
            if self._active_generation is not None:
                self._disconnect_unlocked()
            host = self._normalize_component(config.host, "host")
            self._normalize_component(config.share, "share")
            username = f"{config.domain}\\{config.username}" if config.domain else config.username
            session = smbclient.register_session(
                host,
                username=username,
                password=password,
                port=config.port,
                encrypt=config.require_encryption,
                connection_cache=self._connection_cache,
                auth_protocol=self._auth_protocol,
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
            self._password = password
            return SessionInfo(
                dialect=_DIALECT_NAMES.get(dialect_value, f"0x{dialect_value:04x}"),
                signing=bool(session.signing_required),
                encryption=bool(session.encrypt_data),
                session_generation=self._session_generation,
            )

    def disconnect(self) -> None:
        with self._lifecycle_lock:
            self._ensure_lifecycle_available()
            self._disconnect_unlocked()

    def reset_connection(self) -> None:
        with self._lifecycle_lock:
            self._ensure_lifecycle_available()
            self._disconnect_unlocked()

    def is_generation_current(self, generation: int) -> bool:
        with self._lifecycle_lock:
            return self._active_generation == generation

    @contextmanager
    def session_lease(self, generation: int) -> Iterator[None]:
        with self._lifecycle_lock:
            if self._active_generation != generation:
                raise StaleSmbHandleError("SMB session generation is no longer current")
            if self._lease_owner is not None:
                raise RuntimeError("SMB session lease is already held")
            self._lease_owner = get_ident()
            try:
                yield
            finally:
                self._lease_owner = None

    def _ensure_lifecycle_available(self) -> None:
        if self._lease_owner == get_ident():
            raise RuntimeError("SMB session lifecycle cannot change during a lease")

    def _disconnect_unlocked(self) -> None:
        self._active_generation = None
        self._config = None
        self._password = None
        self._clear_connection_cache()

    def stat(self, path: RemotePath) -> RemoteStat | None:
        try:
            result = smbclient.stat(self._unc(path), **self._session_kwargs())
        except OSError as error:
            if redacted_error_code(error) == "path_not_found":
                return None
            raise
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
                name = validate_remote_component(entry.name)
                child = RemotePath(f"{path.value}/{name}")
                entry_stat = smbclient.stat(self._unc(child), **self._session_kwargs())
                entries.append(
                    RemoteEntry(
                        name=name,
                        is_directory=stat_module.S_ISDIR(entry_stat.st_mode),
                        size=entry_stat.st_size,
                    )
                )
        return entries

    def probe_share(self) -> None:
        """Open and enumerate the share root to prove tree-connect authorization."""
        config = self._require_config()
        host = self._normalize_component(config.host, "host")
        share = self._normalize_component(config.share, "share")
        with smbclient.scandir(
            f"\\\\{host}\\{share}",
            **self._session_kwargs(),
        ) as iterator:
            next(iterator, None)

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
        except Exception as error:
            try:
                target_exists = self.stat(target) is not None
            except Exception:  # noqa: BLE001 - failure to query leaves the outcome unknown
                target_exists = False
            if target_exists or redacted_error_code(error) == "target_exists":
                raise TargetExistsError("exclusive SMB rename target already exists") from error
            raise RenameOutcomeUnknownError("exclusive SMB rename outcome is unknown") from error

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
        body_failed = False
        try:
            yield cast(BinaryIO, stream)
        except BaseException:
            body_failed = True
            raise
        finally:
            try:
                raw.close()
            except OSError as error:
                if not body_failed and self.is_generation_current(generation):
                    _raise_stale_handle_on_bad_descriptor(error)
                    raise

    def _unc(self, path: RemotePath) -> str:
        config = self._require_config()
        host = self._normalize_component(config.host, "host")
        share = self._normalize_component(config.share, "share")
        remote = normalize_remote_path(path.value).value.replace("/", "\\")
        return f"\\\\{host}\\{share}\\{remote}"

    def _session_kwargs(self) -> dict[str, Any]:
        config = self._require_config()
        if self._password is None:
            raise ConnectionError("SMB credentials are unavailable")
        username = f"{config.domain}\\{config.username}" if config.domain else config.username
        return {
            "username": username,
            "password": self._password,
            "port": config.port,
            "auth_protocol": self._auth_protocol,
            "connection_cache": self._connection_cache,
        }

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
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"SMB {field_name} must be a non-blank UNC component")
        if field_name == "host":
            return SmbProtocolGateway._normalize_host(value)
        try:
            return validate_remote_component(value)
        except ValueError as error:
            raise ValueError(f"SMB {field_name} is not a valid UNC component") from error

    @staticmethod
    def _normalize_host(value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("SMB host must not contain control characters")
        if value.startswith("[") and value.endswith("]"):
            candidate = value[1:-1]
            try:
                ipaddress.IPv6Address(candidate)
            except ValueError as error:
                raise ValueError("SMB host has an invalid bracketed IPv6 address") from error
            return value
        if ":" in value:
            try:
                ipaddress.IPv6Address(value)
            except ValueError as error:
                raise ValueError("SMB host has an invalid IPv6 address") from error
            return f"[{value}]"
        if any(char in '<>:"/\\|?*' for char in value):
            raise ValueError("SMB host contains an invalid UNC character")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            address = None
        if isinstance(address, ipaddress.IPv6Address):
            return f"[{value}]"
        if address is not None:
            return value
        if len(value.encode("utf-16-le")) // 2 > 255:
            raise ValueError("SMB host exceeds the conservative UTF-16 component limit")
        labels = value.split(".")
        if any(
            not label
            or len(label) > 63
            or label[0] == "-"
            or label[-1] == "-"
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for char in label)
            for label in labels
        ):
            raise ValueError("SMB host is not a valid hostname, IPv4, or IPv6 address")
        return value
