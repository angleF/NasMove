from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nasmove.core.errors import UnsafeSourceDeletion
from nasmove.core.states import ItemState, TransferAction
from nasmove.transfer.deletion import SourceDeletionService


def test_source_is_not_removed_when_full_verification_is_missing(deletion_fixture) -> None:
    deletion_fixture.replace_item(full_hash_verified=False)
    with pytest.raises(UnsafeSourceDeletion):
        deletion_fixture.service.delete_verified_source(
            deletion_fixture.item.id,
            deletion_fixture.session,
        )
    assert deletion_fixture.local.remove_calls == []


def test_new_session_reverifies_target_before_delete(deletion_fixture) -> None:
    deletion_fixture.replace_item(verified_session_generation=2)
    deletion_fixture.replace_session(generation=3)
    deletion_fixture.service.delete_verified_source(
        deletion_fixture.item.id,
        deletion_fixture.session,
    )
    assert deletion_fixture.verifier.full_verify_calls == 1


class _RepositoryWithoutTask:
    def __init__(self, base) -> None:
        self._base = base

    def get_item(self, item_id):
        return self._base.get_item(item_id)

    def update_item_metadata(self, item, expected_revision):
        return self._base.update_item_metadata(item, expected_revision)

    def transition_item(self, item_id, expected, target):
        return self._base.transition_item(item_id, expected, target)


def test_missing_task_reader_fails_closed_before_source_delete(deletion_fixture) -> None:
    service = SourceDeletionService(
        _RepositoryWithoutTask(deletion_fixture.repository),
        deletion_fixture.local,
        deletion_fixture.remote,
        deletion_fixture.verifier,
    )
    with pytest.raises(UnsafeSourceDeletion):
        service.delete_verified_source(deletion_fixture.item.id, deletion_fixture.session)
    assert deletion_fixture.local.remove_calls == []


@pytest.mark.parametrize(
    "task_value",
    [
        RuntimeError("repository unavailable"),
        None,
        SimpleNamespace(action=TransferAction.COPY),
        SimpleNamespace(action="move"),
        SimpleNamespace(),
    ],
    ids=["read-error", "missing-task", "copy", "wrong-action-type", "missing-action"],
)
def test_task_action_must_be_explicit_move(deletion_fixture, task_value) -> None:
    if isinstance(task_value, BaseException):
        def get_task(_item_task_id):
            raise task_value
    else:
        def get_task(_item_task_id):
            return task_value

    deletion_fixture.repository.get_task = get_task
    with pytest.raises(UnsafeSourceDeletion):
        deletion_fixture.service.delete_verified_source(
            deletion_fixture.item.id,
            deletion_fixture.session,
        )
    assert deletion_fixture.local.remove_calls == []


@pytest.mark.parametrize(
    "verification",
    [
        SimpleNamespace(matches=True, source_unchanged=True),
        SimpleNamespace(matches=True, source_unchanged=True, full_hash_verified=True),
        SimpleNamespace(
            matches=True,
            source_unchanged=True,
            full_hash_verified=True,
            session_generation=2,
        ),
    ],
    ids=["missing-full-hash", "missing-generation", "wrong-generation"],
)
def test_reverification_missing_or_wrong_safety_fields_fails_closed(
    deletion_fixture, verification
) -> None:
    deletion_fixture.replace_item(verified_session_generation=2)
    deletion_fixture.replace_session(generation=3)
    deletion_fixture.verifier.result = verification
    with pytest.raises(UnsafeSourceDeletion):
        deletion_fixture.service.delete_verified_source(
            deletion_fixture.item.id,
            deletion_fixture.session,
        )
    assert deletion_fixture.local.remove_calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"source_hash": "0" * 64},
        {"remote_hash": "0" * 64},
        {"source_bytes": 1},
        {"remote_bytes": 1},
        {"remote_file_id": "other-file"},
    ],
    ids=["source-hash", "remote-hash", "source-length", "remote-length", "file-id"],
)
def test_reverification_binding_mismatch_fails_closed(deletion_fixture, changes) -> None:
    deletion_fixture.replace_item(verified_session_generation=2)
    deletion_fixture.replace_session(generation=3)
    deletion_fixture.verifier.result = replace(
        deletion_fixture.verifier.complete_result(), **changes
    )
    with pytest.raises(UnsafeSourceDeletion):
        deletion_fixture.service.delete_verified_source(
            deletion_fixture.item.id,
            deletion_fixture.session,
        )
    assert deletion_fixture.local.remove_calls == []


def test_delete_requires_committed_target_and_marks_done(deletion_fixture) -> None:
    result = deletion_fixture.service.delete_verified_source(
        deletion_fixture.item.id,
        deletion_fixture.session,
    )
    assert result.source_deleted is True
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.DONE
    assert deletion_fixture.local.fingerprint_calls >= 2


def test_delete_failure_retains_source_and_marks_source_retained(deletion_fixture) -> None:
    deletion_fixture.local.remove_error = PermissionError("denied")
    result = deletion_fixture.service.delete_verified_source(
        deletion_fixture.item.id,
        deletion_fixture.session,
    )
    assert result.source_deleted is False
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.SOURCE_RETAINED


def test_missing_source_with_matching_target_is_idempotently_done(deletion_fixture) -> None:
    deletion_fixture.local.source_exists = False
    result = deletion_fixture.service.delete_verified_source(
        deletion_fixture.item.id,
        deletion_fixture.session,
    )
    assert result.already_absent is True
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.DONE
    assert deletion_fixture.local.remove_calls == []


def test_missing_source_without_full_verification_is_not_assumed_done(deletion_fixture) -> None:
    deletion_fixture.local.source_exists = False
    deletion_fixture.replace_item(full_hash_verified=False)
    with pytest.raises(UnsafeSourceDeletion):
        deletion_fixture.service.delete_verified_source(
            deletion_fixture.item.id,
            deletion_fixture.session,
        )
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.COMMITTED


def test_changed_final_target_is_not_deleted(deletion_fixture) -> None:
    deletion_fixture.remote.files[deletion_fixture.item.final_path.value] = bytearray(b"tampered")
    with pytest.raises(UnsafeSourceDeletion):
        deletion_fixture.service.delete_verified_source(
            deletion_fixture.item.id,
            deletion_fixture.session,
        )
    assert deletion_fixture.local.remove_calls == []


def test_deletion_service_has_expected_public_constructor() -> None:
    assert SourceDeletionService.delete_verified_source
