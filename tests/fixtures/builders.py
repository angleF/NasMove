from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from nasmove.core.model import (
    ConflictPolicy,
    ConnectionConfig,
    ConnectionProfileId,
    DeletionEvidence,
    RemotePath,
    SourceFingerprint,
    TaskId,
    TaskRecord,
    TransferAction,
    TransferItemId,
    TransferItemRecord,
    VerificationPolicy,
)
from nasmove.core.states import ItemState, SourceKind, TaskState


def build_connection_config() -> ConnectionConfig:
    return ConnectionConfig(
        profile_id=ConnectionProfileId("connection-profile-1"),
        display_name="Primary NAS",
        host="nas.example.test",
        share="data",
        username="operator",
    )


def build_source_fingerprint() -> SourceFingerprint:
    return SourceFingerprint(device=1, inode=2, kind=SourceKind.FILE, size=1024, mtime_ns=1)


def build_task_record() -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        id=TaskId("task-1"),
        name="Copy data",
        action=TransferAction.COPY,
        connection=build_connection_config(),
        target_root=RemotePath("target"),
        conflict_policy=ConflictPolicy.AUTO_RENAME,
        verification_policy=VerificationPolicy.FULL,
        state=TaskState.DRAFT,
        queue_position=0,
        recovery_generation=1,
        total_files=1,
        total_bytes=1024,
        copied_bytes=0,
        verified_bytes=0,
        revision=0,
        created_at=now,
        updated_at=now,
    )


def build_transfer_item_record() -> TransferItemRecord:
    return TransferItemRecord(
        id=TransferItemId("item-1"),
        task_id=TaskId("task-1"),
        source_path=Path("/source/file.bin"),
        relative_path=PurePosixPath("file.bin"),
        final_path=RemotePath("target/file.bin"),
        temp_path=RemotePath("target/.file.bin.part"),
        source_fingerprint=build_source_fingerprint(),
        state=ItemState.PLANNED,
    )


def build_deletion_evidence() -> DeletionEvidence:
    return DeletionEvidence(
        source_unchanged=True,
        full_hash_verified=True,
        target_committed=True,
        verified_session_generation=1,
        current_session_generation=1,
        source_fingerprint=build_source_fingerprint(),
        target_path=RemotePath("target/file.bin"),
        sha256="a" * 64,
    )
