from __future__ import annotations

from typing import Any, cast

from nasmove.core.errors import UnsafeCredentialBackend
from nasmove.core.model import ConnectionProfileId

SERVICE = "com.nasmove.smb"
_MACOS_BACKEND_MODULE = "keyring.backends.macOS"


class MacOSKeychainCredentialStore:
    """Store SMB passwords only through the macOS Keychain keyring backend."""

    def __init__(self, keyring_module: Any | None = None) -> None:
        if keyring_module is None:
            import keyring

            keyring_module = keyring
        self._keyring = keyring_module
        self._ensure_safe_backend()

    def _ensure_safe_backend(self) -> None:
        try:
            backend = self._keyring.get_keyring()
            backend_module = type(backend).__module__
        except Exception:  # noqa: BLE001 - backend details must never escape
            raise UnsafeCredentialBackend("macOS Keychain backend is unavailable") from None
        if backend_module != _MACOS_BACKEND_MODULE:
            raise UnsafeCredentialBackend("only the macOS Keychain backend is allowed")

    @staticmethod
    def _account(profile_id: ConnectionProfileId) -> str:
        return str(profile_id)

    def get_password(self, profile_id: ConnectionProfileId) -> str | None:
        self._ensure_safe_backend()
        try:
            return cast(str | None, self._keyring.get_password(SERVICE, self._account(profile_id)))
        except Exception:  # noqa: BLE001 - backend details must never escape
            raise UnsafeCredentialBackend("macOS Keychain read failed") from None

    def set_password(self, profile_id: ConnectionProfileId, password: str) -> None:
        self._ensure_safe_backend()
        try:
            self._keyring.set_password(SERVICE, self._account(profile_id), password)
        except Exception:  # noqa: BLE001 - backend details must never escape
            raise UnsafeCredentialBackend("macOS Keychain write failed") from None

    def delete_password(self, profile_id: ConnectionProfileId) -> None:
        self._ensure_safe_backend()
        try:
            self._keyring.delete_password(SERVICE, self._account(profile_id))
        except Exception:  # noqa: BLE001 - backend details must never escape
            raise UnsafeCredentialBackend("macOS Keychain delete failed") from None
