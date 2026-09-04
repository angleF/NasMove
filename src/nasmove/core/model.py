from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import NewType

from nasmove.core.errors import DomainValidationError
from nasmove.core.states import (
    ConflictPolicy,
    ItemState,
    SourceKind,
    TaskState,
    TransferAction,
    VerificationPolicy,
)

TaskId = NewType("TaskId", str)
TransferItemId = NewType("TransferItemId", str)
ConnectionProfileId = NewType("ConnectionProfileId", str)

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _require_non_negative(value: int, field_name: str) -> None:
    if value < 0:
        raise DomainValidationError(f"{field_name} must not be negative")


def _require_exact_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise DomainValidationError(f"{field_name} must be a bool")


def _require_positive_int(value: object, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        raise DomainValidationError(f"{field_name} must be a positive int")


def _require_sha256(value: str, field_name: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise DomainValidationError(f"{field_name} must be a 64-character lowercase SHA-256")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise DomainValidationError(f"{field_name} must be a timezone-aware UTC datetime")


@dataclass(frozen=True, slots=True)
class RemotePath:
    value: str

    def __post_init__(self) -> None:
        if not self.value or self.value.startswith("/") or "\\" in self.value:
            raise DomainValidationError("remote path must be a normalized relative POSIX path")
        if any(part in {"", ".", ".."} for part in self.value.split("/")):
            raise DomainValidationError("remote path must not contain empty, dot, or dot-dot segments")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    profile_id: ConnectionProfileId
    display_name: str
    host: str
    share: str
    username: str
    port: int = 445
    domain: str | None = None
    require_encryption: bool = True
    minimum_dialect: str = "3.0"

    def __post_init__(self) -> None:
        for field_name, value in (
            ("display_name", self.display_name),
            ("host", self.host),
            ("share", self.share),
            ("username", self.username),
        ):
            if not value:
                raise DomainValidationError(f"{field_name} must not be empty")
        if not 1 <= self.port <= 65535:
            raise DomainValidationError("port must be between 1 and 65535")


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    device: int
    inode: int
    kind: SourceKind
    size: int
    mtime_ns: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("device", self.device),
            ("inode", self.inode),
            ("size", self.size),
            ("mtime_ns", self.mtime_ns),
        ):
            _require_non_negative(value, field_name)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    item_id: TransferItemId
    confirmed_offset: int
    remote_size: int
    window_start: int
    window_length: int
    window_sha256: str
    session_generation: int
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for field_name, value in (
            ("confirmed_offset", self.confirmed_offset),
            ("remote_size", self.remote_size),
            ("window_start", self.window_start),
            ("window_length", self.window_length),
        ):
            _require_non_negative(value, field_name)
        if self.window_start + self.window_length > self.confirmed_offset:
            raise DomainValidationError("checkpoint window must end at or before confirmed offset")
        if self.confirmed_offset > self.remote_size:
            raise DomainValidationError("confirmed offset must not exceed remote size")
        _require_sha256(self.window_sha256, "window_sha256")
        if self.session_generation <= 0:
            raise DomainValidationError("session_generation must be greater than zero")
        _require_utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: TaskId
    name: str
    action: TransferAction
    connection: ConnectionConfig
    target_root: RemotePath
    conflict_policy: ConflictPolicy
    verification_policy: VerificationPolicy
    state: TaskState
    queue_position: int
    recovery_generation: int
    total_files: int
    total_bytes: int
    copied_bytes: int
    verified_bytes: int
    revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for field_name, value in (
            ("queue_position", self.queue_position),
            ("recovery_generation", self.recovery_generation),
            ("total_files", self.total_files),
            ("total_bytes", self.total_bytes),
            ("copied_bytes", self.copied_bytes),
            ("verified_bytes", self.verified_bytes),
            ("revision", self.revision),
        ):
            _require_non_negative(value, field_name)
        if self.copied_bytes > self.total_bytes:
            raise DomainValidationError("copied_bytes must not exceed total_bytes")
        if self.verified_bytes > self.total_bytes:
            raise DomainValidationError("verified_bytes must not exceed total_bytes")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise DomainValidationError("updated_at must not precede created_at")
        if self.action is TransferAction.MOVE and self.verification_policy is not VerificationPolicy.FULL:
            raise DomainValidationError("move tasks require full verification")


@dataclass(frozen=True, slots=True)
class TransferItemRecord:
    id: TransferItemId
    task_id: TaskId
    source_path: Path
    relative_path: PurePosixPath
    final_path: RemotePath
    temp_path: RemotePath
    source_fingerprint: SourceFingerprint
    state: ItemState
    confirmed_offset: int = 0
    retry_count: int = 0
    sha256: str | None = None
    full_hash_verified: bool = False
    target_file_id: str | None = None
    final_size: int | None = None
    committed_at: datetime | None = None
    verified_session_generation: int | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise DomainValidationError("source_path must be absolute")
        if self.relative_path.is_absolute() or not self.relative_path.parts:
            raise DomainValidationError("relative_path must be a non-empty relative path")
        if any(part in {".", ".."} for part in self.relative_path.parts):
            raise DomainValidationError("relative_path must not contain dot or dot-dot segments")
        for field_name, value in (
            ("confirmed_offset", self.confirmed_offset),
            ("retry_count", self.retry_count),
            ("revision", self.revision),
        ):
            _require_non_negative(value, field_name)
        if self.confirmed_offset > self.source_fingerprint.size:
            raise DomainValidationError("confirmed_offset must not exceed source size")
        if self.sha256 is not None:
            _require_sha256(self.sha256, "sha256")
        if self.full_hash_verified and (
            self.sha256 is None or self.verified_session_generation is None
        ):
            raise DomainValidationError(
                "full_hash_verified requires sha256 and verified_session_generation"
            )
        if self.final_size is not None:
            _require_non_negative(self.final_size, "final_size")
        if self.committed_at is not None:
            _require_utc(self.committed_at, "committed_at")


@dataclass(frozen=True, slots=True)
class DeletionEvidence:
    source_unchanged: bool
    full_hash_verified: bool
    target_committed: bool
    verified_session_generation: int
    current_session_generation: int
    source_fingerprint: SourceFingerprint | None = None
    target_path: RemotePath | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("source_unchanged", self.source_unchanged),
            ("full_hash_verified", self.full_hash_verified),
            ("target_committed", self.target_committed),
        ):
            _require_exact_bool(value, field_name)
        _require_positive_int(self.verified_session_generation, "verified_session_generation")
        _require_positive_int(self.current_session_generation, "current_session_generation")


@dataclass(frozen=True, slots=True)
class SourceDeleteAuthorization:
    source_fingerprint: SourceFingerprint
    target_path: RemotePath
    sha256: str
    session_generation: int

    def __post_init__(self) -> None:
        _require_sha256(self.sha256, "sha256")
        _require_positive_int(self.session_generation, "session_generation")


def advance_revision[T: (TaskRecord, TransferItemRecord)](record: T) -> T:
    if not isinstance(record, (TaskRecord, TransferItemRecord)):
        raise TypeError("record must be a TaskRecord or TransferItemRecord")
    return replace(record, revision=record.revision + 1)
