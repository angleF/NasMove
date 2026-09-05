from __future__ import annotations

import importlib
from typing import Any, cast

from nasmove.core.errors import UnsafeCredentialBackend
from nasmove.core.model import ConnectionProfileId

SERVICE = "com.nasmove.smb"
_MACOS_BACKEND_MODULE = "keyring.backends.macOS"


class MacOSKeychainCredentialStore:
    """Store SMB passwords only through the macOS Keychain keyring backend."""

    def __init__(self, keyring_module: Any | None = None) -> None:
        import_failed = False
        if keyring_module is None:
            try:
                import keyring
            except Exception:  # noqa: BLE001 - import details must never escape
                import_failed = True
            else:
                keyring_module = keyring
        if import_failed or keyring_module is None:
            raise UnsafeCredentialBackend("keyring backend is unavailable")
        self._keyring = keyring_module
        backend, backend_error = self._load_backend()
        if backend_error is not None:
            raise UnsafeCredentialBackend(backend_error)
        self._backend: Any = backend

    def _load_backend(self) -> tuple[Any | None, str | None]:
        backend: Any | None = None
        try:
            backend = self._keyring.get_keyring()
            macos_module = importlib.import_module(_MACOS_BACKEND_MODULE)
            official_class = macos_module.Keyring
            is_official = type(backend) is official_class
        except Exception:  # noqa: BLE001 - backend details must never escape
            return None, "macOS Keychain backend is unavailable"
        if not is_official:
            return None, "only the official macOS Keychain backend is allowed"
        return backend, None

    def _ensure_backend(self) -> bool:
        """Keep a defensive check that does not re-resolve the keyring backend."""
        try:
            return type(self._backend).__module__ == _MACOS_BACKEND_MODULE
        except Exception:  # noqa: BLE001 - malformed test doubles fail closed
            return False

    def _call_error(self, operation: str, method_name: str, *args: Any) -> tuple[Any, str | None]:
        call: Any = None
        result: Any = None
        failed = False
        try:
            call = getattr(self._backend, method_name)
            result = call(*args)
        except Exception:  # noqa: BLE001 - backend details must never escape
            failed = True
        if failed:
            return None, f"macOS Keychain {operation} failed"
        return result, None

    @staticmethod
    def _account(profile_id: ConnectionProfileId) -> str:
        return str(profile_id)

    def get_password(self, profile_id: ConnectionProfileId) -> str | None:
        if not self._ensure_backend():
            raise UnsafeCredentialBackend("only the official macOS Keychain backend is allowed")
        result, error = self._call_error(
            "read", "get_password", SERVICE, self._account(profile_id)
        )
        if error is not None:
            raise UnsafeCredentialBackend(error)
        return cast(str | None, result)

    def set_password(self, profile_id: ConnectionProfileId, password: str) -> None:
        if not self._ensure_backend():
            raise UnsafeCredentialBackend("only the official macOS Keychain backend is allowed")
        _, error = self._call_error(
            "write", "set_password", SERVICE, self._account(profile_id), password
        )
        if error is not None:
            raise UnsafeCredentialBackend(error)

    def delete_password(self, profile_id: ConnectionProfileId) -> None:
        if not self._ensure_backend():
            raise UnsafeCredentialBackend("only the official macOS Keychain backend is allowed")
        _, error = self._call_error(
            "delete", "delete_password", SERVICE, self._account(profile_id)
        )
        if error is not None:
            raise UnsafeCredentialBackend(error)
