from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nasmove.core.model import RemotePath


class TransferErrorCategory(StrEnum):
    """Stable, user-facing classes for failures at an external transfer boundary."""

    NETWORK = "network"
    DNS = "dns"
    AUTHENTICATION = "authentication"
    ACCOUNT_LOCKED = "account_locked"
    PERMISSION = "permission"
    DISK_FULL = "disk_full"
    QUOTA = "quota"
    NOT_FOUND = "not_found"
    TARGET_EXISTS = "target_exists"
    LOCKED = "locked"
    INVALID_NAME = "invalid_name"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TransferFailure:
    category: TransferErrorCategory
    code: str
    retryable: bool

    @property
    def error_category(self) -> TransferErrorCategory:
        return self.category

    @property
    def error_code(self) -> str:
        return self.code


class DomainValidationError(ValueError):
    """Raised when a domain value violates a safety invariant."""


class InvalidRemotePath(DomainValidationError):
    """Raised when a remote path is empty, unsafe, or otherwise invalid."""


class InvalidTransition(DomainValidationError):
    """Raised when a state-machine transition is not explicitly allowed."""


class ConcurrentStateChange(DomainValidationError):
    """Raised when a compare-and-swap update observes a stale state."""


class ConnectionProfileInUse(RuntimeError):
    """Raised when an incomplete task still needs a connection profile."""


class PreflightCancelled(RuntimeError):
    """Raised when a cooperative preflight cancellation is observed."""


class ConflictResolutionRequired(DomainValidationError):
    """Raised when an ask-policy conflict needs an explicit user decision."""

    def __init__(self, remote_path: RemotePath) -> None:
        self.remote_path = remote_path
        super().__init__(f"conflict resolution is required for {remote_path.value}")


class UnsafeSourceDeletion(DomainValidationError):
    """Raised when evidence is insufficient to delete a source item."""


class SourceFileMissingError(FileNotFoundError):
    """Raised when a local source path no longer exists by the time it is read.

    A distinct type from a remote ``path_not_found`` so the UI can tell the user
    the source was already consumed instead of blaming the NAS directory.  It
    stays a ``FileNotFoundError`` so every fail-closed "source is gone" path
    keeps its current meaning.
    """
