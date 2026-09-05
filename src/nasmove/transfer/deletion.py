from __future__ import annotations

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
    TransferItemId,
    TransferItemRecord,
)
from nasmove.core.ports import RemoteStat, SessionInfo
from nasmove.core.states import ItemState, SourceKind
from nasmove.core.transitions import authorize_source_delete
from nasmove.localio.hashing import sha256_stream


class TaskRepository(Protocol):
    def get_item(self, item_id: TransferItemId) -> TransferItemRecord: ...

    def update_item_metadata(self, item: TransferItemRecord, expected_revision: int) -> None: ...

    def transition_item(self, item_id: TransferItemId, expected: ItemState, target: ItemState) -> None: ...


class LocalFileGateway(Protocol):
    def fingerprint(self, path: Path) -> SourceFingerprint: ...

    def open_read(self, path: Path) -> AbstractContextManager[BinaryIO]: ...

    def remove_file(
        self, path: Path, expected_fingerprint: SourceFingerprint | None = None
    ) -> None: ...

    def remove_empty_dir(
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

    @property
    def item_state(self) -> ItemState:
        return self.state

    @property
    def deleted(self) -> bool:
        return self.source_deleted


class SourceDeletionService:
    """Delete local sources only after re-checking the committed target."""

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
        item = self._repository.get_item(item_id)
        if item.state is ItemState.DONE:
            return DeletionResult(item.id, ItemState.DONE, True, already_absent=True)
        if item.state not in self._DELETABLE_STATES:
            raise UnsafeSourceDeletion("only committed items may delete a source")
        get_task = getattr(self._repository, "get_task", None)
        if get_task is not None:
            task = get_task(item.task_id)
            if getattr(getattr(task, "action", None), "value", None) == "copy":
                raise UnsafeSourceDeletion("copy tasks must retain their source")

        source_fingerprint = self._read_source_fingerprint(item)
        target_stat, target_hash = self._read_verified_target(item, session)
        if source_fingerprint is None:
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
            return DeletionResult(item.id, ItemState.DONE, True, already_absent=True)

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
            if self._source_exists(current.source_path):
                self._retain_source(current)
                return DeletionResult(current.id, ItemState.SOURCE_RETAINED, False)
        except RuntimeError:
            self._retain_source(current)
            raise
        except Exception as error:  # noqa: BLE001 - deletion failure is a durable result
            self._retain_source(current)
            return DeletionResult(
                current.id,
                ItemState.SOURCE_RETAINED,
                False,
                warnings=(f"source deletion failed: {error}",),
            )

        if self._source_exists(current.source_path):
            self._retain_source(current)
            return DeletionResult(
                current.id,
                ItemState.SOURCE_RETAINED,
                False,
                warnings=("source still exists after deletion",),
            )

        self._repository.transition_item(current.id, ItemState.SOURCE_DELETE_AUTHORIZED, ItemState.DONE)
        removed_dirs, warnings = self._clean_planned_directories()
        return DeletionResult(
            current.id,
            ItemState.DONE,
            True,
            directories_removed=removed_dirs,
            warnings=warnings,
        )

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
            and target_stat.size == expected_size
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
        if item.source_fingerprint.kind is SourceKind.EMPTY_DIRECTORY:
            self._local.remove_empty_dir(item.source_path, item.source_fingerprint)
        else:
            self._local.remove_file(item.source_path, item.source_fingerprint)

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
                self._local.remove_empty_dir(directory)
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
