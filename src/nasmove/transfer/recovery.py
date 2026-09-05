from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Protocol

from nasmove.core.model import (
    Checkpoint,
    RemotePath,
    SourceFingerprint,
    TransferItemId,
    TransferItemRecord,
)
from nasmove.core.ports import RemoteStat, SessionInfo
from nasmove.localio.hashing import sha256_range, sha256_stream


class RecoveryDisposition(StrEnum):
    RESUME = "resume"
    START_OVER = "start_over"
    SOURCE_CHANGED = "source_changed"
    FINAL_CONFIRMED = "final_confirmed"
    FINAL_UNSAFE = "final_unsafe"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    safe_offset: int
    truncate_remote: bool
    disposition: RecoveryDisposition
    checkpoint: Checkpoint | None = None
    error: Exception | None = None

    @property
    def offset(self) -> int:
        return self.safe_offset

    @property
    def recovery_disposition(self) -> RecoveryDisposition:
        return self.disposition


class TaskRepository(Protocol):
    def get_item(self, item_id: TransferItemId) -> TransferItemRecord: ...

    def checkpoints_desc(self, item_id: TransferItemId) -> list[Checkpoint]: ...


class LocalFileGateway(Protocol):
    def fingerprint(self, path: Path) -> SourceFingerprint: ...

    def open_read(self, path: Path) -> AbstractContextManager[BinaryIO]: ...


class SmbGateway(Protocol):
    def is_generation_current(self, generation: int) -> bool: ...

    def session_lease(self, generation: int) -> AbstractContextManager[None]: ...

    def stat(self, path: RemotePath) -> RemoteStat | None: ...

    def open_read(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...

    def truncate(self, path: RemotePath, size: int) -> None: ...

    def rename_exclusive(self, source: RemotePath, target: RemotePath) -> None: ...


class RecoveryCoordinator:
    """Reconcile durable checkpoints with the current source and SMB state."""

    def __init__(
        self,
        repository: TaskRepository,
        local_gateway: LocalFileGateway,
        smb_gateway: SmbGateway,
        session: SessionInfo | None = None,
        *,
        session_generation: int | None = None,
    ) -> None:
        if session is not None and session_generation is not None:
            raise ValueError("provide either session or session_generation")
        if session is None:
            if session_generation is None or session_generation <= 0:
                raise ValueError("a positive current session generation is required")
            session = SessionInfo("unknown", False, False, session_generation)
        self._repository = repository
        self._local = local_gateway
        self._smb = smb_gateway
        self._session = session

    def find_safe_offset(self, item_id: TransferItemId) -> RecoveryDecision:
        item = self._repository.get_item(item_id)
        current_fingerprint = self._local.fingerprint(item.source_path)
        if current_fingerprint != item.source_fingerprint:
            return RecoveryDecision(0, False, RecoveryDisposition.SOURCE_CHANGED)

        checkpoints = self._repository.checkpoints_desc(item_id)
        with self._lease():
            final_stat = self._smb.stat(item.final_path)
            if final_stat is not None:
                return self._check_final_file(item, final_stat, current_fingerprint)

            temporary_stat = self._smb.stat(item.temp_path)
            if temporary_stat is None:
                return RecoveryDecision(0, False, RecoveryDisposition.START_OVER)
            if temporary_stat.is_directory:
                return self._isolate_and_start_over(item, ValueError("temporary path is a directory"))

            for checkpoint in checkpoints:
                if not self._usable_checkpoint(checkpoint, item, temporary_stat.size):
                    continue
                local_hash = self._window_hash_local(item, checkpoint)
                remote_hash = self._window_hash_remote(item, checkpoint)
                if local_hash != checkpoint.window_sha256 or remote_hash != checkpoint.window_sha256:
                    continue
                if self._local.fingerprint(item.source_path) != item.source_fingerprint:
                    return RecoveryDecision(0, False, RecoveryDisposition.SOURCE_CHANGED)
                should_truncate = temporary_stat.size > checkpoint.confirmed_offset
                if should_truncate:
                    self._smb.truncate(item.temp_path, checkpoint.confirmed_offset)
                return RecoveryDecision(
                    checkpoint.confirmed_offset,
                    should_truncate,
                    RecoveryDisposition.RESUME,
                    checkpoint=checkpoint,
                )

            return self._isolate_and_start_over(item)

    def _check_final_file(
        self,
        item: TransferItemRecord,
        final_stat: RemoteStat,
        expected_fingerprint: SourceFingerprint,
    ) -> RecoveryDecision:
        if final_stat.is_directory or final_stat.size != expected_fingerprint.size:
            return RecoveryDecision(0, False, RecoveryDisposition.FINAL_UNSAFE)
        with self._local.open_read(item.source_path) as source:
            local_result = sha256_stream(source)
        if self._local.fingerprint(item.source_path) != expected_fingerprint:
            return RecoveryDecision(0, False, RecoveryDisposition.SOURCE_CHANGED)
        with self._smb.open_read(item.final_path) as remote:
            remote_result = sha256_stream(remote)
        if self._local.fingerprint(item.source_path) != expected_fingerprint:
            return RecoveryDecision(0, False, RecoveryDisposition.SOURCE_CHANGED)
        if (
            local_result.byte_count != remote_result.byte_count
            or local_result.hexdigest != remote_result.hexdigest
        ):
            return RecoveryDecision(0, False, RecoveryDisposition.FINAL_UNSAFE)
        return RecoveryDecision(expected_fingerprint.size, False, RecoveryDisposition.FINAL_CONFIRMED)

    @staticmethod
    def _usable_checkpoint(
        checkpoint: Checkpoint, item: TransferItemRecord, remote_size: int
    ) -> bool:
        return (
            checkpoint.item_id == item.id
            and checkpoint.confirmed_offset <= remote_size
            and checkpoint.confirmed_offset <= item.confirmed_offset
            and checkpoint.confirmed_offset <= item.source_fingerprint.size
            and checkpoint.window_start + checkpoint.window_length <= checkpoint.confirmed_offset
        )

    def _window_hash_local(self, item: TransferItemRecord, checkpoint: Checkpoint) -> str:
        with self._local.open_read(item.source_path) as source:
            return sha256_range(source, checkpoint.window_start, checkpoint.window_length)

    def _window_hash_remote(self, item: TransferItemRecord, checkpoint: Checkpoint) -> str:
        with self._smb.open_read(item.temp_path) as remote:
            return sha256_range(remote, checkpoint.window_start, checkpoint.window_length)

    def _isolate_and_start_over(
        self, item: TransferItemRecord, error: Exception | None = None
    ) -> RecoveryDecision:
        temporary_stat = self._smb.stat(item.temp_path)
        if temporary_stat is None:
            return RecoveryDecision(0, False, RecoveryDisposition.START_OVER, error=error)
        for index in range(1000):
            suffix = ".orphan" if index == 0 else f".orphan-{index}"
            candidate = type(item.temp_path)(item.temp_path.value + suffix)
            try:
                self._smb.rename_exclusive(item.temp_path, candidate)
            except FileExistsError:
                continue
            except Exception as isolation_error:  # noqa: BLE001 - preserve both safety and cause
                return RecoveryDecision(0, False, RecoveryDisposition.START_OVER, error=isolation_error)
            return RecoveryDecision(0, False, RecoveryDisposition.START_OVER, error=error)
        return RecoveryDecision(
            0,
            False,
            RecoveryDisposition.START_OVER,
            error=RuntimeError("unable to isolate stale temporary file"),
        )

    def _lease(self) -> AbstractContextManager[None]:
        generation = self._session.session_generation
        if not self._smb.is_generation_current(generation):
            raise OSError("SMB session generation is not current")
        return self._smb.session_lease(generation)


__all__ = ["RecoveryCoordinator", "RecoveryDecision", "RecoveryDisposition"]
