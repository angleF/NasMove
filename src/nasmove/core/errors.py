from dataclasses import dataclass
from enum import StrEnum


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


class UnsafeSourceDeletion(DomainValidationError):
    """Raised when evidence is insufficient to delete a source item."""


class UnsafeCredentialBackend(DomainValidationError):
    """Raised when credentials cannot be stored in the macOS Keychain backend."""
