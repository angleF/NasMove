from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import BinaryIO, Protocol

from nasmove.core.model import RemotePath, TransferItemRecord
from nasmove.core.ports import RemoteEntry, RemoteStat, SessionInfo
from nasmove.core.states import ItemState, SourceKind
from nasmove.localio.hashing import sha256_stream
from nasmove.planning.conflicts import allocate_name
from nasmove.transfer.verification import VerificationResult


class TaskRepository(Protocol):
    def get_item(self, item_id: object) -> TransferItemRecord: ...

    def update_item_metadata(self, item: TransferItemRecord, expected_revision: int) -> None: ...

    def transition_item(self, item_id: object, expected: ItemState, target: ItemState) -> None: ...


class SmbGateway(Protocol):
    def is_generation_current(self, generation: int) -> bool: ...

    def session_lease(self, generation: int) -> AbstractContextManager[None]: ...

    def stat(self, path: RemotePath) -> RemoteStat | None: ...

    def list_dir(self, path: RemotePath) -> list[RemoteEntry]: ...

    def rename_exclusive(self, source: RemotePath, target: RemotePath) -> None: ...

    def open_read(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...


@dataclass(frozen=True, slots=True)
class CommitResult:
    final_path: RemotePath
    final_size: int
    target_file_id: str | None
    committed: bool = True


class TargetCommitter:
    """Publish a verified temporary file using an exclusive same-directory rename."""

    def __init__(
        self,
        repository: TaskRepository,
        smb_gateway: SmbGateway,
        session: SessionInfo | None = None,
        *,
        session_generation: int | None = None,
    ) -> None:
        if session is not None and session_generation is not None:
            raise ValueError("provide either session or session_generation")
        if session is None:
            if session_generation is None or session_generation <= 0:
                raise ValueError("a positive session generation is required")
            session = SessionInfo("unknown", False, False, session_generation)
        self._repository = repository
        self._smb = smb_gateway
        self._session = session

    def commit(self, item: TransferItemRecord, verification: VerificationResult) -> CommitResult:
        if verification.matches is not True or verification.source_unchanged is not True:
            raise ValueError("commit requires matching verification and unchanged source")
        if verification.session_generation != self._session.session_generation:
            raise ValueError("verification session generation is stale")
        if item.state is not ItemState.VERIFIED:
            raise ValueError("only verified items can be committed")
        current = self._repository.get_item(item.id)
        if current.state is not ItemState.VERIFIED:
            raise ValueError("item state changed before commit")

        with self._lease():
            candidate = self._choose_initial_path(current)
            while True:
                if candidate != current.final_path:
                    current = self._persist_path(current, candidate)
                temporary_stat = self._smb.stat(current.temp_path)
                is_directory = current.source_fingerprint.kind is SourceKind.EMPTY_DIRECTORY
                if temporary_stat is None or temporary_stat.is_directory is not is_directory:
                    raise OSError("verified temporary object is missing or has the wrong type")
                if (
                    verification.remote_file_id is not None
                    and temporary_stat.file_id != verification.remote_file_id
                ):
                    raise OSError("temporary file identity changed after verification")
                try:
                    self._assert_generation()
                    self._smb.rename_exclusive(current.temp_path, candidate)
                    self._assert_generation()
                except FileExistsError:
                    candidate = self._next_name(candidate)
                    continue

                final_stat = self._smb.stat(candidate)
                if (
                    final_stat is None
                    or final_stat.is_directory is not is_directory
                    or (not is_directory and final_stat.size != verification.source_bytes)
                ):
                    raise OSError("renamed target could not be confirmed")
                if not self._target_identity_is_safe(final_stat, verification):
                    raise OSError("renamed target identity does not match verified temporary file")
                if verification.remote_file_id is None and not is_directory:
                    self._verify_final_content(candidate, verification)
                if is_directory and self._smb.list_dir(candidate):
                    raise OSError("renamed target directory is no longer empty")
                final_size = 0 if is_directory else final_stat.size
                current = replace(
                    current,
                    final_path=candidate,
                    sha256=verification.source_hash,
                    full_hash_verified=True,
                    target_file_id=final_stat.file_id,
                    final_size=final_size,
                    committed_at=datetime.now(UTC),
                    verified_session_generation=verification.session_generation,
                )
                self._repository.update_item_metadata(current, current.revision)
                self._repository.transition_item(
                    current.id, ItemState.VERIFIED, ItemState.COMMITTED
                )
                return CommitResult(candidate, final_size, final_stat.file_id)

    def _choose_initial_path(self, item: TransferItemRecord) -> RemotePath:
        if self._smb.stat(item.final_path) is None:
            return item.final_path
        return self._next_name(item.final_path)

    def _target_identity_is_safe(
        self, final_stat: RemoteStat, verification: VerificationResult
    ) -> bool:
        expected_id = verification.remote_file_id
        if expected_id is None:
            return True
        return final_stat.file_id == expected_id

    def _verify_final_content(
        self, path: RemotePath, verification: VerificationResult
    ) -> None:
        with self._smb.open_read(path) as stream:
            stream.seek(0)
            result = sha256_stream(stream)
        if (
            result.byte_count != verification.source_bytes
            or result.byte_count != verification.remote_bytes
            or result.hexdigest != verification.source_hash
            or result.hexdigest != verification.remote_hash
        ):
            raise OSError("renamed target content no longer matches verification")

    def _next_name(self, path: RemotePath) -> RemotePath:
        parent, name = self._parent_and_name(path)
        occupied = {name}
        try:
            occupied.update(entry.name for entry in self._smb.list_dir(parent))
        except (AttributeError, FileNotFoundError):
            pass
        allocated = allocate_name(name, occupied)
        return RemotePath(f"{parent.value}/{allocated}" if parent.value else allocated)

    def _persist_path(self, item: TransferItemRecord, path: RemotePath) -> TransferItemRecord:
        updated = replace(item, final_path=path)
        self._repository.update_item_metadata(updated, item.revision)
        return replace(updated, revision=item.revision + 1)

    def _lease(self) -> AbstractContextManager[None]:
        self._assert_generation()
        return self._smb.session_lease(self._session.session_generation)

    def _assert_generation(self) -> None:
        if not self._smb.is_generation_current(self._session.session_generation):
            raise OSError("SMB session generation is not current")

    @staticmethod
    def _parent_and_name(path: RemotePath) -> tuple[RemotePath, str]:
        parsed = PurePosixPath(path.value)
        name = parsed.name
        parent_value = str(parsed.parent)
        if parent_value == ".":
            raise ValueError("final path must include a remote parent directory")
        return RemotePath(parent_value), name


__all__ = ["CommitResult", "TargetCommitter"]
