from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from nasmove.core.errors import DomainValidationError, UnsafeSourceDeletion
from nasmove.core.model import (
    Checkpoint,
    ConflictPolicy,
    ConnectionConfig,
    ConnectionProfileId,
    DeletionEvidence,
    RemotePath,
    SourceDeleteAuthorization,
    SourceFingerprint,
    TaskId,
    TaskRecord,
    TransferAction,
    TransferItemId,
    TransferItemRecord,
    VerificationPolicy,
    advance_revision,
)
from nasmove.core.states import ItemState, SourceKind, TaskState
from nasmove.core.transitions import authorize_source_delete

SHA256 = "a" * 64


def _fingerprint() -> SourceFingerprint:
    return SourceFingerprint(device=1, inode=2, kind=SourceKind.FILE, size=10, mtime_ns=3)


def _evidence(**changes: object) -> DeletionEvidence:
    values: dict[str, object] = {
        "source_unchanged": True,
        "full_hash_verified": True,
        "target_committed": True,
        "verified_session_generation": 4,
        "current_session_generation": 4,
        "source_fingerprint": _fingerprint(),
        "target_path": RemotePath("target/file.bin"),
        "sha256": SHA256,
    }
    values.update(changes)
    return DeletionEvidence(**values)  # type: ignore[arg-type]


def _connection() -> ConnectionConfig:
    return ConnectionConfig(
        profile_id=ConnectionProfileId("profile-1"),
        display_name="NAS",
        host="nas.example.test",
        share="data",
        username="operator",
    )


def test_session_generation_change_revokes_delete_authorization() -> None:
    evidence = DeletionEvidence(
        source_unchanged=True,
        full_hash_verified=True,
        target_committed=True,
        verified_session_generation=4,
        current_session_generation=5,
    )
    with pytest.raises(UnsafeSourceDeletion):
        authorize_source_delete(evidence)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"source_unchanged": False}, "source changed"),
        ({"full_hash_verified": False}, "full hash missing"),
        ({"target_committed": False}, "target uncommitted"),
        ({"verified_session_generation": 1, "current_session_generation": 2}, "generation"),
        ({"source_fingerprint": None}, "source context missing"),
        ({"target_path": None}, "target context missing"),
        ({"sha256": None}, "hash context missing"),
        ({"sha256": "A" * 64}, "non-canonical hash"),
    ],
)
def test_unsafe_evidence_is_rejected(changes: dict[str, object], reason: str) -> None:
    with pytest.raises(UnsafeSourceDeletion):
        authorize_source_delete(_evidence(**changes))


def test_valid_evidence_returns_immutable_authorization_with_evidence_values() -> None:
    evidence = _evidence()

    authorization = authorize_source_delete(evidence)

    assert authorization.source_fingerprint is evidence.source_fingerprint
    assert authorization.target_path is evidence.target_path
    assert authorization.sha256 == SHA256
    assert authorization.session_generation == 4
    with pytest.raises(FrozenInstanceError):
        authorization.sha256 = "b" * 64  # type: ignore[misc]


@pytest.mark.parametrize(
    "field", ["source_unchanged", "full_hash_verified", "target_committed"]
)
def test_deletion_evidence_rejects_truthy_non_boolean_safety_flags(field: str) -> None:
    with pytest.raises(DomainValidationError):
        _evidence(**{field: "false"})


@pytest.mark.parametrize(
    "field", ["source_unchanged", "full_hash_verified", "target_committed"]
)
def test_authorization_defensively_rejects_truthy_non_boolean_safety_flags(field: str) -> None:
    evidence = _evidence()
    object.__setattr__(evidence, field, "false")

    with pytest.raises(UnsafeSourceDeletion):
        authorize_source_delete(evidence)


@pytest.mark.parametrize("field", ["verified_session_generation", "current_session_generation"])
@pytest.mark.parametrize("value", [0, -1, True])
def test_deletion_evidence_rejects_non_positive_or_non_integer_generations(
    field: str, value: int | bool
) -> None:
    with pytest.raises(DomainValidationError):
        _evidence(**{field: value})


@pytest.mark.parametrize("value", [0, -1, True])
def test_source_delete_authorization_rejects_invalid_session_generation(value: int | bool) -> None:
    with pytest.raises(DomainValidationError):
        SourceDeleteAuthorization(
            source_fingerprint=_fingerprint(),
            target_path=RemotePath("target/file.bin"),
            sha256=SHA256,
            session_generation=value,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"verified_session_generation": True, "current_session_generation": 1},
        {"verified_session_generation": 0, "current_session_generation": 0},
        {"verified_session_generation": -1, "current_session_generation": -1},
        {"verified_session_generation": 1, "current_session_generation": True},
        {"verified_session_generation": 1, "current_session_generation": 0},
        {"verified_session_generation": 1, "current_session_generation": -1},
    ],
)
def test_authorization_defensively_rejects_tampered_invalid_generations(
    changes: dict[str, int | bool],
) -> None:
    evidence = _evidence()
    for field, value in changes.items():
        object.__setattr__(evidence, field, value)

    with pytest.raises(UnsafeSourceDeletion):
        authorize_source_delete(evidence)


@pytest.mark.parametrize("value", ["", "/absolute", "back\\slash", "a//b", "a/./b", "a/../b"])
def test_remote_path_rejects_non_normalized_relative_posix_paths(value: str) -> None:
    with pytest.raises(DomainValidationError):
        RemotePath(value)


def test_remote_path_returns_its_value_when_converted_to_text() -> None:
    assert str(RemotePath("directory/file.bin")) == "directory/file.bin"


def test_connection_config_has_no_password_and_validates_its_boundary_values() -> None:
    connection = _connection()

    assert not hasattr(connection, "password")
    with pytest.raises(DomainValidationError):
        ConnectionConfig(
            profile_id=ConnectionProfileId("profile-1"),
            display_name="",
            host="nas.example.test",
            share="data",
            username="operator",
        )
    with pytest.raises(DomainValidationError):
        ConnectionConfig(
            profile_id=ConnectionProfileId("profile-1"),
            display_name="NAS",
            host="nas.example.test",
            port=0,
            share="data",
            username="operator",
        )


def test_checkpoint_rejects_invalid_offset_bounds_and_hashes() -> None:
    with pytest.raises(DomainValidationError):
        Checkpoint(
            item_id=TransferItemId("item-1"),
            confirmed_offset=4,
            remote_size=3,
            window_start=0,
            window_length=4,
            window_sha256=SHA256,
            session_generation=1,
        )
    with pytest.raises(DomainValidationError):
        Checkpoint(
            item_id=TransferItemId("item-1"),
            confirmed_offset=4,
            remote_size=4,
            window_start=0,
            window_length=4,
            window_sha256="not-a-sha256",
            session_generation=1,
        )


def test_move_task_uses_the_only_permitted_full_verification_policy() -> None:
    now = datetime.now(UTC)
    record = TaskRecord(
        id=TaskId("task-1"),
        name="Move files",
        action=TransferAction.MOVE,
        connection=_connection(),
        target_root=RemotePath("target"),
        conflict_policy=ConflictPolicy.AUTO_RENAME,
        verification_policy=VerificationPolicy.FULL,
        state=TaskState.DRAFT,
        queue_position=0,
        recovery_generation=0,
        total_files=0,
        total_bytes=0,
        copied_bytes=0,
        verified_bytes=0,
        revision=0,
        created_at=now,
        updated_at=now,
    )

    assert record.verification_policy is VerificationPolicy.FULL
    assert list(VerificationPolicy) == [VerificationPolicy.FULL]


def test_transfer_item_rejects_offset_past_source_size_and_incomplete_hash_proof() -> None:
    with pytest.raises(DomainValidationError):
        TransferItemRecord(
            id=TransferItemId("item-1"),
            task_id=TaskId("task-1"),
            source_path=Path("/source/file.bin"),
            relative_path=PurePosixPath("file.bin"),
            final_path=RemotePath("target/file.bin"),
            temp_path=RemotePath("target/.file.bin.part"),
            source_fingerprint=_fingerprint(),
            state=ItemState.TRANSFERRING,
            confirmed_offset=11,
        )
    with pytest.raises(DomainValidationError):
        TransferItemRecord(
            id=TransferItemId("item-1"),
            task_id=TaskId("task-1"),
            source_path=Path("/source/file.bin"),
            relative_path=PurePosixPath("file.bin"),
            final_path=RemotePath("target/file.bin"),
            temp_path=RemotePath("target/.file.bin.part"),
            source_fingerprint=_fingerprint(),
            state=ItemState.VERIFIED,
            full_hash_verified=True,
        )


def test_advance_revision_returns_a_new_task_record_without_mutating_the_original() -> None:
    now = datetime.now(UTC)
    record = TaskRecord(
        id=TaskId("task-1"),
        name="Copy files",
        action=TransferAction.COPY,
        connection=_connection(),
        target_root=RemotePath("target"),
        conflict_policy=ConflictPolicy.AUTO_RENAME,
        verification_policy=VerificationPolicy.FULL,
        state=TaskState.DRAFT,
        queue_position=0,
        recovery_generation=1,
        total_files=1,
        total_bytes=10,
        copied_bytes=0,
        verified_bytes=0,
        revision=7,
        created_at=now,
        updated_at=now,
    )

    advanced = advance_revision(record)

    assert isinstance(advanced, TaskRecord)
    assert advanced.revision == 8
    assert record.revision == 7
    assert advanced is not record


def test_advance_revision_returns_a_new_transfer_item_without_mutating_the_original() -> None:
    record = TransferItemRecord(
        id=TransferItemId("item-1"),
        task_id=TaskId("task-1"),
        source_path=Path("/source/file.bin"),
        relative_path=PurePosixPath("file.bin"),
        final_path=RemotePath("target/file.bin"),
        temp_path=RemotePath("target/.file.bin.part"),
        source_fingerprint=_fingerprint(),
        state=ItemState.PLANNED,
        revision=3,
    )

    advanced = advance_revision(record)

    assert isinstance(advanced, TransferItemRecord)
    assert advanced.revision == 4
    assert record.revision == 3
    assert advanced is not record


def test_advance_revision_rejects_non_record_objects() -> None:
    with pytest.raises(TypeError):
        advance_revision(object())  # type: ignore[arg-type]
