from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from nasmove.core.errors import UnsafeSourceDeletion
from nasmove.core.model import (
    DeletionEvidence,
    RemotePath,
    SourceFingerprint,
    TaskRecord,
    TransferItemId,
    TransferItemRecord,
)
from nasmove.core.ports import RemoteStat, SessionInfo
from nasmove.core.states import ItemState, SourceKind, TransferAction
from nasmove.core.transitions import authorize_source_delete
from nasmove.localio.hashing import sha256_stream

_EMPTY_HASH = hashlib.sha256().hexdigest()

# Human-readable diagnosis for the durable deletion outcome record. The code is
# stored in ``events.error_code`` and the text in ``events.summary``.
_REASON_SUMMARY = {
    "source_deleted": "source moved to system Trash",
    "source_already_done": "source deletion was already recorded",
    "source_already_absent": "source was already absent; committed target verified",
    "source_missing_after_move": "source was absent after a reported Trash failure; committed target verified",
    "source_retained_move_failed": "moving source to Trash failed",
    "source_retained_still_exists": "source still exists after the Trash operation",
}


class TaskRepository(Protocol):
    def get_task(self, task_id: object) -> TaskRecord: ...

    def get_item(self, item_id: TransferItemId) -> TransferItemRecord: ...

    def update_item_metadata(self, item: TransferItemRecord, expected_revision: int) -> None: ...

    def transition_item(self, item_id: TransferItemId, expected: ItemState, target: ItemState) -> None: ...


class LocalFileGateway(Protocol):
    def fingerprint(self, path: Path) -> SourceFingerprint: ...

    def open_read(self, path: Path) -> AbstractContextManager[BinaryIO]: ...

    def move_to_trash(
        self, path: Path, expected_fingerprint: SourceFingerprint | None = None
    ) -> None: ...


class SmbGateway(Protocol):
    def is_generation_current(self, generation: int) -> bool: ...

    def session_lease(self, generation: int) -> AbstractContextManager[None]: ...

    def stat(self, path: RemotePath) -> RemoteStat | None: ...

    def open_read(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...


class IntegrityVerifier(Protocol):
    def verify_full(self, item: TransferItemRecord) -> object: ...


@dataclass(frozen=True, slots=True)
class DeletionResult:
    """The durable outcome of a source deletion attempt."""

    item_id: TransferItemId
    state: ItemState
    source_deleted: bool
    already_absent: bool = False
    directories_removed: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()
    reason: str = ""

    @property
    def item_state(self) -> ItemState:
        return self.state

    @property
    def deleted(self) -> bool:
        return self.source_deleted


class SourceDeletionService:
    """Move local sources to Trash only after re-checking the committed target."""

    _DELETABLE_STATES = frozenset(
        {ItemState.COMMITTED, ItemState.SOURCE_DELETE_AUTHORIZED, ItemState.SOURCE_RETAINED}
    )

    def __init__(
        self,
        repository: TaskRepository,
        local_gateway: LocalFileGateway,
        smb_gateway: SmbGateway,
        verifier: IntegrityVerifier,
        planned_source_directories: Iterable[Path] = (),
    ) -> None:
        self._repository = repository
        self._local = local_gateway
        self._smb = smb_gateway
        self._verifier = verifier
        self._planned_source_directories = tuple(planned_source_directories)

    def delete_verified_source(
        self, item_id: TransferItemId, session: SessionInfo
    ) -> DeletionResult:
        try:
            result = self._delete_verified_source(item_id, session)
        except UnsafeSourceDeletion as error:
            # A refused deletion would otherwise leave the item retained with no
            # recorded reason at all; record why, then re-raise unchanged.
            self._record_refusal(item_id, error)
            raise
        self._record_outcome(result)
        return result

    def _delete_verified_source(
        self, item_id: TransferItemId, session: SessionInfo
    ) -> DeletionResult:
        item = self._repository.get_item(item_id)
        if item.state is ItemState.DONE:
            return DeletionResult(
                item.id, ItemState.DONE, True, already_absent=True, reason="source_already_done"
            )
        if item.state not in self._DELETABLE_STATES:
            raise UnsafeSourceDeletion("only committed items may delete a source")
        try:
            task = self._repository.get_task(item.task_id)
            action = task.action
        except Exception as error:
            raise UnsafeSourceDeletion("unable to confirm task action before source deletion") from error
        if type(action) is not TransferAction or action is not TransferAction.MOVE:
            raise UnsafeSourceDeletion("only move tasks may delete their source")

        source_fingerprint = self._read_source_fingerprint(item)
        target_stat, target_hash = self._read_verified_target(item, session)
        if source_fingerprint is None:
            return self._recover_missing_source(item, target_hash, "source_already_absent")

        if source_fingerprint != item.source_fingerprint:
            raise UnsafeSourceDeletion("source changed before deletion")

        verified_generation = item.verified_session_generation
        verification = None
        if verified_generation != session.session_generation:
            verification = self._reverify(item, session)
            if not self._verification_is_safe(
                verification, session.session_generation, item, target_stat, target_hash
            ):
                raise UnsafeSourceDeletion("full verification did not confirm the committed target")
            verified_generation = session.session_generation

        if target_hash != item.sha256:
            raise UnsafeSourceDeletion("committed target digest does not match persisted digest")
        if item.sha256 is None:
            raise UnsafeSourceDeletion("SHA-256 is required for source deletion")

        current = self._repository.get_item(item.id)
        if current != item:
            raise UnsafeSourceDeletion("task item changed while deletion was being authorized")

        evidence = DeletionEvidence(
            source_unchanged=True,
            full_hash_verified=item.full_hash_verified,
            target_committed=True,
            verified_session_generation=verified_generation or 0,
            current_session_generation=session.session_generation,
            source_fingerprint=item.source_fingerprint,
            target_path=item.final_path,
            sha256=item.sha256,
        )
        authorize_source_delete(evidence)

        if current.state is not ItemState.SOURCE_DELETE_AUTHORIZED:
            self._repository.transition_item(current.id, current.state, ItemState.SOURCE_DELETE_AUTHORIZED)
            current = self._repository.get_item(item.id)

        if verification is not None:
            current = self._persist_reverification(current, verification, target_stat, session)

        try:
            self._remove_source(current)
        except FileNotFoundError:
            # The Trash entry may already exist even though the call reported a
            # miss; only retain the source when it is genuinely still present.
            if self._source_exists(current.source_path):
                self._retain_source(current)
                return DeletionResult(
                    current.id,
                    ItemState.SOURCE_RETAINED,
                    False,
                    reason="source_retained_move_failed",
                )
            return self._recover_missing_source(
                current, target_hash, "source_missing_after_move", clean_directories=True
            )
        except RuntimeError:
            self._retain_source(current)
            raise
        except Exception as error:  # noqa: BLE001 - deletion failure is a durable result
            if not self._source_exists(current.source_path):
                # Qt can report a failed Trash move after the file has already
                # left its original path; the durable state must not claim the
                # source is still retained.  Reuses the reviewed recovery gate.
                return self._recover_missing_source(
                    current, target_hash, "source_missing_after_move", clean_directories=True
                )
            self._retain_source(current)
            return DeletionResult(
                current.id,
                ItemState.SOURCE_RETAINED,
                False,
                warnings=(f"moving source to Trash failed: {error}",),
                reason="source_retained_move_failed",
            )

        if self._source_exists(current.source_path):
            self._retain_source(current)
            return DeletionResult(
                current.id,
                ItemState.SOURCE_RETAINED,
                False,
                warnings=("source still exists after Trash operation",),
                reason="source_retained_still_exists",
            )

        self._repository.transition_item(current.id, ItemState.SOURCE_DELETE_AUTHORIZED, ItemState.DONE)
        removed_dirs, warnings = self._clean_planned_directories()
        return DeletionResult(
            current.id,
            ItemState.DONE,
            True,
            directories_removed=removed_dirs,
            warnings=warnings,
            reason="source_deleted",
        )

    def _recover_missing_source(
        self,
        item: TransferItemRecord,
        target_hash: str | None,
        reason: str,
        *,
        clean_directories: bool = False,
    ) -> DeletionResult:
        """Reuse the reviewed "source is already gone" safety path.

        Every gate here is the one the top-of-method missing-source path has
        always applied: full hash verification, a non-null stored digest, and a
        committed target whose digest matches it.  Nothing is weakened.

        ``clean_directories`` restores the baseline behaviour of the
        post-failure branches, which fell through to the planned-directory
        cleanup; the entry-time path returned before it and keeps that.
        """
        if item.full_hash_verified is not True or item.sha256 is None:
            raise UnsafeSourceDeletion("full verification is required before recovery")
        if target_hash != item.sha256:
            raise UnsafeSourceDeletion("missing source has a mismatching committed target")
        current = self._repository.get_item(item.id)
        if current != item:
            raise UnsafeSourceDeletion("task item changed while recovery was being checked")
        if current.state is ItemState.SOURCE_RETAINED:
            self._repository.transition_item(
                current.id, ItemState.SOURCE_RETAINED, ItemState.SOURCE_DELETE_AUTHORIZED
            )
            current = self._repository.get_item(item.id)
        self._repository.transition_item(current.id, current.state, ItemState.DONE)
        directories_removed: tuple[Path, ...] = ()
        warnings: tuple[str, ...] = ()
        if clean_directories:
            directories_removed, warnings = self._clean_planned_directories()
        return DeletionResult(
            item.id,
            ItemState.DONE,
            True,
            already_absent=True,
            directories_removed=directories_removed,
            warnings=warnings,
            reason=reason,
        )

    def _record_outcome(self, result: DeletionResult) -> None:
        """Persist the outcome so a later "why is it retained?" is answerable.

        Diagnostics can never change the deletion outcome: any recorder failure
        is swallowed and the already-decided result is returned unchanged.
        """
        recorder = getattr(self._repository, "record_deletion_outcome", None)
        if not callable(recorder) or not result.reason:
            return
        summary = _REASON_SUMMARY.get(result.reason, result.reason)
        if result.warnings:
            summary = f"{summary}: {result.warnings[0]}"
        try:
            item = self._repository.get_item(result.item_id)
            recorder(item.task_id, result.item_id, result.reason, summary)
        except Exception:  # noqa: BLE001 - the deletion result is already durable
            return

    def _record_refusal(self, item_id: TransferItemId, error: BaseException) -> None:
        """Persist why a deletion was refused, then let the refusal propagate.

        Without this the item is left retained with no recorded reason, which is
        the exact combination the deletion-outcome record exists to prevent.
        """
        recorder = getattr(self._repository, "record_deletion_outcome", None)
        if not callable(recorder):
            return
        try:
            item = self._repository.get_item(item_id)
            recorder(
                item.task_id,
                item_id,
                "deletion_refused",
                f"source deletion was refused: {error}",
            )
        except Exception:  # noqa: BLE001 - the refusal itself is the durable outcome
            return

    def _read_source_fingerprint(self, item: TransferItemRecord) -> SourceFingerprint | None:
        try:
            return self._local.fingerprint(item.source_path)
        except FileNotFoundError:
            return None

    def _read_verified_target(
        self, item: TransferItemRecord, session: SessionInfo
    ) -> tuple[RemoteStat, str | None]:
        if not self._smb.is_generation_current(session.session_generation):
            raise UnsafeSourceDeletion("SMB session generation is not current")
        with self._smb.session_lease(session.session_generation):
            target = self._smb.stat(item.final_path)
            if item.source_fingerprint.kind is SourceKind.EMPTY_DIRECTORY:
                if target is None or not target.is_directory:
                    raise UnsafeSourceDeletion("committed target directory is missing")
                if item.target_file_id is not None and target.file_id != item.target_file_id:
                    raise UnsafeSourceDeletion("committed target identity changed")
                return target, _EMPTY_HASH
            if target is None or target.is_directory:
                raise UnsafeSourceDeletion("committed target is missing or not a file")
            if item.final_size is not None and target.size != item.final_size:
                raise UnsafeSourceDeletion("committed target size changed")
            if target.size != item.source_fingerprint.size:
                raise UnsafeSourceDeletion("committed target size does not match source")
            if item.target_file_id is not None and target.file_id != item.target_file_id:
                raise UnsafeSourceDeletion("committed target identity changed")
            with self._smb.open_read(item.final_path) as remote:
                digest = sha256_stream(remote).hexdigest
        return target, digest

    def _reverify(self, item: TransferItemRecord, session: SessionInfo) -> object:
        if not self._smb.is_generation_current(session.session_generation):
            raise UnsafeSourceDeletion("SMB session generation is not current")
        # IntegrityVerifier historically hashes ``temp_path``; at deletion time the
        # committed object lives at final_path, so bind the verifier to that object.
        verification_item = replace(item, temp_path=item.final_path)
        return self._verifier.verify_full(verification_item)

    @staticmethod
    def _verification_is_safe(
        verification: object,
        expected_generation: int,
        item: TransferItemRecord,
        target_stat: RemoteStat,
        target_hash: str | None,
    ) -> bool:
        matches = getattr(verification, "matches", None)
        unchanged = getattr(verification, "source_unchanged", None)
        full_hash_verified = getattr(verification, "full_hash_verified", None)
        generation = getattr(verification, "session_generation", None)
        source_hash = getattr(verification, "source_hash", None)
        remote_hash = getattr(verification, "remote_hash", None)
        source_bytes = getattr(verification, "source_bytes", None)
        remote_bytes = getattr(verification, "remote_bytes", None)
        remote_file_id = getattr(verification, "remote_file_id", None)
        expected_size = item.source_fingerprint.size
        return (
            type(matches) is bool
            and matches is True
            and type(unchanged) is bool
            and unchanged is True
            and type(full_hash_verified) is bool
            and full_hash_verified is True
            and type(generation) is int
            and generation == expected_generation
            and type(source_hash) is str
            and type(remote_hash) is str
            and item.sha256 is not None
            and source_hash == item.sha256
            and remote_hash == item.sha256
            and remote_hash == target_hash
            and type(source_bytes) is int
            and type(remote_bytes) is int
            and source_bytes == expected_size
            and remote_bytes == expected_size
            and (
                item.source_fingerprint.kind is SourceKind.EMPTY_DIRECTORY
                or target_stat.size == expected_size
            )
            and type(remote_file_id) is type(item.target_file_id)
            and remote_file_id == item.target_file_id
        )

    def _persist_reverification(
        self,
        item: TransferItemRecord,
        verification: object,
        target_stat: RemoteStat,
        session: SessionInfo,
    ) -> TransferItemRecord:
        updated = replace(
            item,
            sha256=getattr(verification, "source_hash", item.sha256),
            full_hash_verified=True,
            target_file_id=target_stat.file_id,
            final_size=target_stat.size,
            verified_session_generation=session.session_generation,
        )
        self._repository.update_item_metadata(updated, item.revision)
        return replace(updated, revision=item.revision + 1)

    def _remove_source(self, item: TransferItemRecord) -> None:
        self._local.move_to_trash(item.source_path, item.source_fingerprint)

    def _source_exists(self, path: Path) -> bool:
        try:
            self._local.fingerprint(path)
        except FileNotFoundError:
            return False
        return True

    def _retain_source(self, item: TransferItemRecord) -> None:
        current = self._repository.get_item(item.id)
        if current.state is ItemState.SOURCE_DELETE_AUTHORIZED:
            self._repository.transition_item(
                current.id, ItemState.SOURCE_DELETE_AUTHORIZED, ItemState.SOURCE_RETAINED
            )

    def _clean_planned_directories(self) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        removed: list[Path] = []
        warnings: list[str] = []
        directories = sorted(set(self._planned_source_directories), key=lambda path: len(path.parts), reverse=True)
        for directory in directories:
            entries = self._list_directory(directory)
            if entries:
                warnings.append(f"source directory retained because it is not empty: {directory}")
                continue
            try:
                self._local.move_to_trash(directory)
            except FileNotFoundError:
                continue
            except OSError as error:
                warnings.append(f"source directory retained: {directory}: {error}")
                continue
            removed.append(directory)
        return tuple(removed), tuple(warnings)

    def _list_directory(self, path: Path) -> Sequence[object]:
        list_dir = getattr(self._local, "list_dir", None)
        if list_dir is not None:
            list_directory = cast(Callable[[Path], Sequence[object]], list_dir)
            return list_directory(path)
        try:
            return tuple(path.iterdir())
        except FileNotFoundError:
            return ()


__all__ = ["DeletionResult", "SourceDeletionService"]
