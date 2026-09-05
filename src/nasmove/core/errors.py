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
