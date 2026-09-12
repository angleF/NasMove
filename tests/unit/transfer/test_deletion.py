from __future__ import annotations

import errno
import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from nasmove.core.errors import UnsafeSourceDeletion
from nasmove.core.states import ItemState, SourceKind, TransferAction
from nasmove.transfer.deletion import SourceDeletionService


def test_source_is_not_removed_when_full_verification_is_missing(deletion_fixture) -> None:
    deletion_fixture.replace_item(full_hash_verified=False)
    with pytest.raises(UnsafeSourceDeletion):
        deletion_fixture.service.delete_verified_source(
            deletion_fixture.item.id,
            deletion_fixture.session,
        )
    assert deletion_fixture.local.trash_calls == []


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
    assert deletion_fixture.local.trash_calls == []


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
    assert deletion_fixture.local.trash_calls == []


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
    assert deletion_fixture.local.trash_calls == []


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
    assert deletion_fixture.local.trash_calls == [deletion_fixture.item.source_path]
    assert deletion_fixture.local.remove_calls == []
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


def test_source_already_trashed_despite_reported_move_failure_is_marked_done(deletion_fixture) -> None:
    # The Qt Trash call reported failure, but the source had in fact already left
    # its original path.  The persisted state must not claim it is still retained.
    deletion_fixture.local.remove_error_still_moves_source = True
    deletion_fixture.local.remove_error = OSError(errno.EIO, "system trash rejected source")

    result = deletion_fixture.service.delete_verified_source(
        deletion_fixture.item.id,
        deletion_fixture.session,
    )

    assert result.state is ItemState.DONE
    assert result.source_deleted is True
    assert result.already_absent is True
    assert deletion_fixture.local.source_exists is False
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.DONE


def test_reported_move_failure_with_source_present_keeps_the_safety_path(deletion_fixture) -> None:
    deletion_fixture.local.remove_error = PermissionError("trash denied")

    result = deletion_fixture.service.delete_verified_source(
        deletion_fixture.item.id,
        deletion_fixture.session,
    )

    assert result.state is ItemState.SOURCE_RETAINED
    assert result.source_deleted is False
    assert result.reason == "source_retained_move_failed"
    assert deletion_fixture.local.source_exists is True
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.SOURCE_RETAINED


def test_source_missing_after_failed_move_is_recorded_as_done(deletion_fixture) -> None:
    deletion_fixture.local.remove_error_still_moves_source = True
    deletion_fixture.local.remove_error = OSError(errno.EIO, "system trash rejected source")

    result = deletion_fixture.service.delete_verified_source(
        deletion_fixture.item.id,
        deletion_fixture.session,
    )

    assert result.reason == "source_missing_after_move"
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.DONE


def test_deletion_outcome_reason_is_reported_to_the_repository(deletion_fixture) -> None:
    recorded: list[tuple[object, ...]] = []

    class RecordingRepository:
        def __init__(self, base) -> None:
            self._base = base

        def record_deletion_outcome(self, task_id, item_id, code, summary) -> None:
            recorded.append((task_id, item_id, code, summary))

        def __getattr__(self, name):
            return getattr(self._base, name)

    service = SourceDeletionService(
        RecordingRepository(deletion_fixture.repository),
        deletion_fixture.local,
        deletion_fixture.remote,
        deletion_fixture.verifier,
    )
    service.delete_verified_source(deletion_fixture.item.id, deletion_fixture.session)

    assert recorded == [
        (
            deletion_fixture.item.task_id,
            deletion_fixture.item.id,
            "source_deleted",
            "source moved to system Trash",
        )
    ]


def test_deletion_outcome_recording_failure_does_not_change_the_deletion_result(deletion_fixture) -> None:
    class BrokenRecorder:
        def __init__(self, base) -> None:
            self._base = base

        def record_deletion_outcome(self, *_args) -> None:
            raise sqlite3.OperationalError("database is locked")

        def __getattr__(self, name):
            return getattr(self._base, name)

    service = SourceDeletionService(
        BrokenRecorder(deletion_fixture.repository),
        deletion_fixture.local,
        deletion_fixture.remote,
        deletion_fixture.verifier,
    )

    result = service.delete_verified_source(deletion_fixture.item.id, deletion_fixture.session)

    assert result.state is ItemState.DONE
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.DONE


def test_missing_source_with_matching_target_is_idempotently_done(deletion_fixture) -> None:
    deletion_fixture.local.source_exists = False
    result = deletion_fixture.service.delete_verified_source(
        deletion_fixture.item.id,
        deletion_fixture.session,
    )
    assert result.already_absent is True
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.DONE
    assert deletion_fixture.local.trash_calls == []


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
    assert deletion_fixture.local.trash_calls == []


def test_deletion_service_has_expected_public_constructor() -> None:
    assert SourceDeletionService.delete_verified_source


def test_committed_empty_directory_is_rechecked_then_removed(deletion_fixture) -> None:
    digest = hashlib.sha256().hexdigest()
    item = replace(
        deletion_fixture.item,
        source_fingerprint=replace(
            deletion_fixture.item.source_fingerprint,
            kind=SourceKind.EMPTY_DIRECTORY,
            size=0,
        ),
        sha256=digest,
        final_size=0,
    )
    deletion_fixture.local.fingerprint_override = item.source_fingerprint
    deletion_fixture.remote.files.pop(item.final_path.value)
    deletion_fixture.remote.directories.add(item.final_path.value)
    deletion_fixture.remote.file_ids[item.final_path.value] = item.target_file_id
    deletion_fixture.repository.items[item.id] = item

    result = deletion_fixture.service.delete_verified_source(item.id, deletion_fixture.session)

    assert result.source_deleted is True
    assert deletion_fixture.repository.get_item(item.id).state is ItemState.DONE


def test_real_repository_persists_and_returns_the_deletion_outcome(deletion_fixture, tmp_path) -> None:
    from nasmove.persistence.sqlite_repository import SqliteTaskRepository
    from tests.fixtures.builders import build_task_record

    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    try:
        task = replace(build_task_record(), action=TransferAction.MOVE)
        item = replace(deletion_fixture.item, state=ItemState.PLANNED)
        repository.create_task(task, [item])
        for expected, target in (
            (ItemState.PLANNED, ItemState.TRANSFERRING),
            (ItemState.TRANSFERRING, ItemState.TRANSFERRED),
            (ItemState.TRANSFERRED, ItemState.VERIFYING),
            (ItemState.VERIFYING, ItemState.VERIFIED),
            (ItemState.VERIFIED, ItemState.COMMITTED),
        ):
            repository.transition_item(item.id, expected, target)

        service = SourceDeletionService(
            repository,
            deletion_fixture.local,
            deletion_fixture.remote,
            deletion_fixture.verifier,
        )

        result = service.delete_verified_source(item.id, deletion_fixture.session)

        assert result.state is ItemState.DONE
        assert repository.last_deletion_outcome(item.id) == (
            "source_deleted",
            "source moved to system Trash",
        )
    finally:
        repository.close()


def test_refused_deletion_records_the_reason_before_raising(deletion_fixture) -> None:
    recorded: list[tuple[object, ...]] = []

    class RecordingRepository:
        def __init__(self, base) -> None:
            self._base = base

        def record_deletion_outcome(self, task_id, item_id, code, summary) -> None:
            recorded.append((task_id, item_id, code, summary))

        def __getattr__(self, name):
            return getattr(self._base, name)

    service = SourceDeletionService(
        RecordingRepository(deletion_fixture.repository),
        deletion_fixture.local,
        deletion_fixture.remote,
        deletion_fixture.verifier,
    )
    deletion_fixture.replace_item(full_hash_verified=False)

    with pytest.raises(UnsafeSourceDeletion):
        service.delete_verified_source(deletion_fixture.item.id, deletion_fixture.session)

    assert len(recorded) == 1
    assert recorded[0][:3] == (
        deletion_fixture.item.task_id,
        deletion_fixture.item.id,
        "deletion_refused",
    )
    assert recorded[0][3].startswith("source deletion was refused")


def test_recovery_cleans_planned_empty_source_directories(deletion_fixture, tmp_path) -> None:
    directory = tmp_path / "planned-source-dir"

    class RecoveryLocal:
        """A source that leaves its path during a Trash call reported as failed."""

        def __init__(self, base) -> None:
            self._base = base
            self.present = True
            self.trashed_directories: list[Path] = []

        def fingerprint(self, path):
            if not self.present:
                raise FileNotFoundError("source is absent", path)
            return self._base.fingerprint(path)

        def move_to_trash(self, path, expected_fingerprint=None) -> None:
            if path == directory:
                self.trashed_directories.append(path)
                return
            self._base.move_to_trash(path, expected_fingerprint)
            self.present = False
            raise OSError(errno.EIO, "system trash rejected source")

        def list_dir(self, path):
            return []

    local = RecoveryLocal(deletion_fixture.local)
    service = SourceDeletionService(
        deletion_fixture.repository,
        local,
        deletion_fixture.remote,
        deletion_fixture.verifier,
        (directory,),
    )

    result = service.delete_verified_source(deletion_fixture.item.id, deletion_fixture.session)

    assert result.state is ItemState.DONE
    assert result.directories_removed == (directory,)
    assert local.trashed_directories == [directory]
