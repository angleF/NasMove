from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nasmove.core.model import ConnectionConfig, ConnectionProfileId


class ConnectionProfileRepository(Protocol):
    def list_connection_profiles(self) -> tuple[ConnectionConfig, ...]: ...

    def get_connection_profile(
        self, profile_id: ConnectionProfileId
    ) -> ConnectionConfig: ...

    def archive_connection_profile(self, profile_id: ConnectionProfileId) -> None: ...


class CredentialStore(Protocol):
    def delete_password(self, profile_id: ConnectionProfileId) -> None: ...


@dataclass(frozen=True, slots=True)
class ProfileArchiveResult:
    archived: bool
    credential_removed: bool
    warning_code: str | None = None


class ConnectionProfileService:
    """Coordinate profile storage with best-effort credential cleanup."""

    def __init__(
        self,
        repository: ConnectionProfileRepository,
        credential_store: CredentialStore,
    ) -> None:
        self._repository = repository
        self._credential_store = credential_store

    def list_profiles(self) -> tuple[ConnectionConfig, ...]:
        return self._repository.list_connection_profiles()

    def get_profile(self, profile_id: ConnectionProfileId) -> ConnectionConfig:
        return self._repository.get_connection_profile(profile_id)

    def archive(self, profile_id: ConnectionProfileId) -> ProfileArchiveResult:
        self._repository.archive_connection_profile(profile_id)
        try:
            self._credential_store.delete_password(profile_id)
        except Exception:  # noqa: BLE001 - credentials and backend details stay private
            return ProfileArchiveResult(True, False, "credential_cleanup_failed")
        return ProfileArchiveResult(True, True)


__all__ = ["ConnectionProfileService", "ProfileArchiveResult"]
