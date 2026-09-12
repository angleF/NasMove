from __future__ import annotations

from typing import Protocol

from nasmove.core.model import ConnectionProfileId


class CredentialRepository(Protocol):
    """The narrow repository surface the credential store depends on."""

    def get_credential_password(self, profile_id: ConnectionProfileId) -> str | None: ...

    def set_credential_password(
        self, profile_id: ConnectionProfileId, password: str
    ) -> None: ...

    def delete_credential_password(self, profile_id: ConnectionProfileId) -> None: ...


class SqliteCredentialStore:
    """Store SMB passwords in the application's own ``credentials`` table.

    The store keeps the same three-method interface the rest of the app already
    consumes; only the backend changes from the macOS Keychain to SQLite.  The
    password is a value and never travels back through a ``ConnectionConfig``.
    """

    def __init__(self, repository: CredentialRepository) -> None:
        self._repository = repository

    def get_password(self, profile_id: ConnectionProfileId) -> str | None:
        return self._repository.get_credential_password(profile_id)

    def set_password(self, profile_id: ConnectionProfileId, password: str) -> None:
        self._repository.set_credential_password(profile_id, password)

    def delete_password(self, profile_id: ConnectionProfileId) -> None:
        self._repository.delete_credential_password(profile_id)


__all__ = ["SqliteCredentialStore"]
