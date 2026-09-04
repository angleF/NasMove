class DomainValidationError(ValueError):
    """Raised when a domain value violates a safety invariant."""


class InvalidTransition(DomainValidationError):
    """Raised when a state-machine transition is not explicitly allowed."""


class UnsafeSourceDeletion(DomainValidationError):
    """Raised when evidence is insufficient to delete a source item."""
