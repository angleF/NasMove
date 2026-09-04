from enum import StrEnum


class TaskState(StrEnum):
    DRAFT = "draft"
    PREFLIGHT = "preflight"
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    WAITING_FOR_NETWORK = "waiting_for_network"
    PAUSED = "paused"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    DELETING_SOURCE = "deleting_source"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELED = "canceled"


class ItemState(StrEnum):
    PLANNED = "planned"
    TRANSFERRING = "transferring"
    TRANSFERRED = "transferred"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    COMMITTED = "committed"
    SOURCE_DELETE_AUTHORIZED = "source_delete_authorized"
    DONE = "done"
    INTERRUPTED = "interrupted"
    WAITING_RETRY = "waiting_retry"
    SOURCE_CHANGED = "source_changed"
    VERIFY_FAILED = "verify_failed"
    SOURCE_RETAINED = "source_retained"
    SKIPPED = "skipped"


class TransferAction(StrEnum):
    COPY = "copy"
    MOVE = "move"


class ConflictPolicy(StrEnum):
    AUTO_RENAME = "auto_rename"


class VerificationPolicy(StrEnum):
    FULL = "full"


class SourceKind(StrEnum):
    FILE = "file"
    EMPTY_DIRECTORY = "empty_directory"
