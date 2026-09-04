from itertools import product

import pytest

from nasmove.core.errors import InvalidTransition
from nasmove.core.states import ItemState, TaskState
from nasmove.core.transitions import assert_item_transition, assert_task_transition


def test_unverified_item_cannot_jump_to_delete_authorized() -> None:
    with pytest.raises(InvalidTransition):
        assert_item_transition(ItemState.TRANSFERRED, ItemState.SOURCE_DELETE_AUTHORIZED)


ITEM_ALLOWED_TRANSITIONS: dict[ItemState, set[ItemState]] = {
    ItemState.PLANNED: {ItemState.TRANSFERRING, ItemState.SKIPPED, ItemState.SOURCE_CHANGED},
    ItemState.TRANSFERRING: {
        ItemState.TRANSFERRED,
        ItemState.INTERRUPTED,
        ItemState.WAITING_RETRY,
        ItemState.SOURCE_CHANGED,
    },
    ItemState.INTERRUPTED: {
        ItemState.TRANSFERRING,
        ItemState.WAITING_RETRY,
        ItemState.SOURCE_CHANGED,
    },
    ItemState.WAITING_RETRY: {
        ItemState.TRANSFERRING,
        ItemState.INTERRUPTED,
        ItemState.SOURCE_CHANGED,
    },
    ItemState.TRANSFERRED: {
        ItemState.VERIFYING,
        ItemState.INTERRUPTED,
        ItemState.SOURCE_CHANGED,
    },
    ItemState.VERIFYING: {
        ItemState.VERIFIED,
        ItemState.VERIFY_FAILED,
        ItemState.INTERRUPTED,
        ItemState.WAITING_RETRY,
        ItemState.SOURCE_CHANGED,
    },
    ItemState.VERIFY_FAILED: {
        ItemState.TRANSFERRING,
        ItemState.VERIFYING,
        ItemState.SOURCE_CHANGED,
    },
    ItemState.VERIFIED: {ItemState.COMMITTED, ItemState.INTERRUPTED, ItemState.SOURCE_CHANGED},
    ItemState.COMMITTED: {
        ItemState.SOURCE_DELETE_AUTHORIZED,
        ItemState.DONE,
        ItemState.SOURCE_RETAINED,
    },
    ItemState.SOURCE_DELETE_AUTHORIZED: {
        ItemState.DONE,
        ItemState.SOURCE_RETAINED,
        ItemState.INTERRUPTED,
    },
    ItemState.SOURCE_RETAINED: {ItemState.SOURCE_DELETE_AUTHORIZED},
}


TASK_ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.DRAFT: {TaskState.PREFLIGHT, TaskState.CANCELED},
    TaskState.PREFLIGHT: {TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELED},
    TaskState.QUEUED: {TaskState.RUNNING, TaskState.PAUSED, TaskState.CANCELED},
    TaskState.RUNNING: {
        TaskState.INTERRUPTED,
        TaskState.WAITING_FOR_NETWORK,
        TaskState.PAUSED,
        TaskState.VERIFYING,
        TaskState.FAILED,
        TaskState.CANCELED,
    },
    TaskState.INTERRUPTED: {
        TaskState.PREFLIGHT,
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.WAITING_FOR_NETWORK,
        TaskState.PAUSED,
        TaskState.FAILED,
        TaskState.CANCELED,
    },
    TaskState.WAITING_FOR_NETWORK: {
        TaskState.RUNNING,
        TaskState.PAUSED,
        TaskState.FAILED,
        TaskState.CANCELED,
    },
    TaskState.PAUSED: {TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELED},
    TaskState.VERIFYING: {
        TaskState.COMMITTING,
        TaskState.INTERRUPTED,
        TaskState.WAITING_FOR_NETWORK,
        TaskState.PAUSED,
        TaskState.FAILED,
    },
    TaskState.COMMITTING: {
        TaskState.DELETING_SOURCE,
        TaskState.COMPLETED,
        TaskState.COMPLETED_WITH_WARNINGS,
        TaskState.INTERRUPTED,
        TaskState.FAILED,
    },
    TaskState.DELETING_SOURCE: {
        TaskState.COMPLETED,
        TaskState.COMPLETED_WITH_WARNINGS,
        TaskState.INTERRUPTED,
        TaskState.FAILED,
    },
}


@pytest.mark.parametrize("current,target", tuple(product(ItemState, ItemState)))
def test_item_transitions_match_the_explicit_graph(current: ItemState, target: ItemState) -> None:
    if target in ITEM_ALLOWED_TRANSITIONS.get(current, set()):
        assert_item_transition(current, target)
    else:
        with pytest.raises(InvalidTransition):
            assert_item_transition(current, target)


@pytest.mark.parametrize("current,target", tuple(product(TaskState, TaskState)))
def test_task_transitions_match_the_explicit_graph(current: TaskState, target: TaskState) -> None:
    if target in TASK_ALLOWED_TRANSITIONS.get(current, set()):
        assert_task_transition(current, target)
    else:
        with pytest.raises(InvalidTransition):
            assert_task_transition(current, target)
