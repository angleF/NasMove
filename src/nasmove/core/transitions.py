from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from nasmove.core.errors import InvalidTransition, UnsafeSourceDeletion
from nasmove.core.model import DeletionEvidence, SourceDeleteAuthorization
from nasmove.core.states import ItemState, TaskState

ITEM_TRANSITIONS: Final[Mapping[ItemState, frozenset[ItemState]]] = MappingProxyType(
    {
        ItemState.PLANNED: frozenset(
            {ItemState.TRANSFERRING, ItemState.SKIPPED, ItemState.SOURCE_CHANGED}
        ),
        ItemState.TRANSFERRING: frozenset(
            {
                ItemState.TRANSFERRED,
                ItemState.INTERRUPTED,
                ItemState.WAITING_RETRY,
                ItemState.SOURCE_CHANGED,
            }
        ),
        ItemState.INTERRUPTED: frozenset(
            {ItemState.TRANSFERRING, ItemState.WAITING_RETRY, ItemState.SOURCE_CHANGED}
        ),
        ItemState.WAITING_RETRY: frozenset(
            {ItemState.TRANSFERRING, ItemState.INTERRUPTED, ItemState.SOURCE_CHANGED}
        ),
        ItemState.TRANSFERRED: frozenset(
            {ItemState.VERIFYING, ItemState.INTERRUPTED, ItemState.SOURCE_CHANGED}
        ),
        ItemState.VERIFYING: frozenset(
            {
                ItemState.VERIFIED,
                ItemState.VERIFY_FAILED,
                ItemState.INTERRUPTED,
                ItemState.WAITING_RETRY,
                ItemState.SOURCE_CHANGED,
            }
        ),
        ItemState.VERIFY_FAILED: frozenset(
            {ItemState.TRANSFERRING, ItemState.VERIFYING, ItemState.SOURCE_CHANGED}
        ),
        ItemState.VERIFIED: frozenset(
            {ItemState.COMMITTED, ItemState.INTERRUPTED, ItemState.SOURCE_CHANGED}
        ),
        ItemState.COMMITTED: frozenset(
            {ItemState.SOURCE_DELETE_AUTHORIZED, ItemState.DONE, ItemState.SOURCE_RETAINED}
        ),
        ItemState.SOURCE_DELETE_AUTHORIZED: frozenset(
            {ItemState.DONE, ItemState.SOURCE_RETAINED, ItemState.INTERRUPTED}
        ),
        ItemState.SOURCE_RETAINED: frozenset({ItemState.SOURCE_DELETE_AUTHORIZED}),
    }
)

TASK_TRANSITIONS: Final[Mapping[TaskState, frozenset[TaskState]]] = MappingProxyType(
    {
        TaskState.DRAFT: frozenset({TaskState.PREFLIGHT, TaskState.CANCELED}),
        TaskState.PREFLIGHT: frozenset({TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELED}),
        TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.PAUSED, TaskState.CANCELED}),
        TaskState.RUNNING: frozenset(
            {
                TaskState.INTERRUPTED,
                TaskState.WAITING_FOR_NETWORK,
                TaskState.PAUSED,
                TaskState.VERIFYING,
                TaskState.FAILED,
                TaskState.CANCELED,
            }
        ),
        TaskState.INTERRUPTED: frozenset(
            {
                TaskState.PREFLIGHT,
                TaskState.QUEUED,
                TaskState.RUNNING,
                TaskState.WAITING_FOR_NETWORK,
                TaskState.PAUSED,
                TaskState.FAILED,
                TaskState.CANCELED,
            }
        ),
        TaskState.WAITING_FOR_NETWORK: frozenset(
            {TaskState.RUNNING, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELED}
        ),
        TaskState.PAUSED: frozenset({TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELED}),
        TaskState.VERIFYING: frozenset(
            {
                TaskState.COMMITTING,
                TaskState.INTERRUPTED,
                TaskState.WAITING_FOR_NETWORK,
                TaskState.PAUSED,
                TaskState.FAILED,
            }
        ),
        TaskState.COMMITTING: frozenset(
            {
                TaskState.DELETING_SOURCE,
                TaskState.COMPLETED,
                TaskState.COMPLETED_WITH_WARNINGS,
                TaskState.INTERRUPTED,
                TaskState.FAILED,
            }
        ),
        TaskState.DELETING_SOURCE: frozenset(
            {
                TaskState.COMPLETED,
                TaskState.COMPLETED_WITH_WARNINGS,
                TaskState.INTERRUPTED,
                TaskState.FAILED,
            }
        ),
    }
)


def assert_item_transition(current: ItemState, target: ItemState) -> None:
    if target not in ITEM_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(f"item transition from {current!s} to {target!s} is not allowed")


def assert_task_transition(current: TaskState, target: TaskState) -> None:
    if target not in TASK_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(f"task transition from {current!s} to {target!s} is not allowed")


def authorize_source_delete(evidence: DeletionEvidence) -> SourceDeleteAuthorization:
    if not evidence.source_unchanged:
        raise UnsafeSourceDeletion("source changed after transfer began")
    if not evidence.full_hash_verified:
        raise UnsafeSourceDeletion("full source and target hash verification is required")
    if not evidence.target_committed:
        raise UnsafeSourceDeletion("target must be committed before source deletion")
    if evidence.verified_session_generation != evidence.current_session_generation:
        raise UnsafeSourceDeletion("session generation changed after verification")
    if evidence.source_fingerprint is None:
        raise UnsafeSourceDeletion("source fingerprint is required for source deletion")
    if evidence.target_path is None:
        raise UnsafeSourceDeletion("target path is required for source deletion")
    if evidence.sha256 is None:
        raise UnsafeSourceDeletion("SHA-256 is required for source deletion")
    try:
        return SourceDeleteAuthorization(
            source_fingerprint=evidence.source_fingerprint,
            target_path=evidence.target_path,
            sha256=evidence.sha256,
            session_generation=evidence.current_session_generation,
        )
    except ValueError as error:
        raise UnsafeSourceDeletion("source deletion requires a canonical SHA-256") from error
