from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from nasmove.core.model import RemotePath, SourceFingerprint, TransferItemRecord
from nasmove.core.ports import RemoteEntry, RemoteStat, SessionInfo
from nasmove.core.states import SourceKind
from nasmove.localio.hashing import HashResult, sha256_stream

_EMPTY_HASH = hashlib.sha256().hexdigest()


class TaskRepository(Protocol):
    def get_item(self, item_id: object) -> TransferItemRecord: ...


class LocalFileGateway(Protocol):
    def fingerprint(self, path: Path) -> SourceFingerprint: ...

    def open_read(self, path: Path) -> AbstractContextManager[BinaryIO]: ...


class SmbGateway(Protocol):
    def is_generation_current(self, generation: int) -> bool: ...

    def session_lease(self, generation: int) -> AbstractContextManager[None]: ...

    def stat(self, path: RemotePath) -> RemoteStat | None: ...

    def open_read(self, path: RemotePath) -> AbstractContextManager[BinaryIO]: ...

    def list_dir(self, path: RemotePath) -> list[RemoteEntry]: ...


@dataclass(frozen=True, slots=True)
class VerificationResult:
    matches: bool
    source_unchanged: bool
    source_hash: str
    remote_hash: str
    source_bytes: int
    remote_bytes: int
    session_generation: int
    remote_file_id: str | None = None

    @property
    def sha256(self) -> str:
        return self.source_hash

    @property
    def source_digest(self) -> str:
        return self.source_hash

    @property
    def remote_digest(self) -> str:
        return self.remote_hash

    @property
    def temporary_file_id(self) -> str | None:
        return self.remote_file_id

    @property
    def full_hash_verified(self) -> bool:
        return self.matches and self.source_unchanged


class IntegrityVerifier:
    """Re-open both ends after transfer and compare complete streams."""

    def __init__(
        self,
        repository: TaskRepository,
        local_gateway: LocalFileGateway,
        smb_gateway: SmbGateway,
        session: SessionInfo | None = None,
        *,
        session_generation: int | None = None,
        progress: Callable[[TransferItemRecord, int], None] | None = None,
    ) -> None:
        if session is not None and session_generation is not None:
            raise ValueError("provide either session or session_generation")
        if session is None:
            if session_generation is None or session_generation <= 0:
                raise ValueError("a positive session generation is required")
            session = SessionInfo("unknown", False, False, session_generation)
        self._repository = repository
        self._local = local_gateway
        self._smb = smb_gateway
        self._session = session
        self._progress = progress

    def verify_full(
        self,
        item: TransferItemRecord,
        token: object | None = None,
    ) -> VerificationResult:
        self._raise_if_stopped(token)
        expected = item.source_fingerprint
        if expected.kind is SourceKind.EMPTY_DIRECTORY:
            return self._verify_empty_directory(item, token)
        before = self._local.fingerprint(item.source_path)
        self._raise_if_stopped(token)
        with self._local.open_read(item.source_path) as source:
            source.seek(0)
            source_result = sha256_stream(
                source,
                progress=lambda _offset: self._raise_if_stopped(token),
            )
        self._raise_if_stopped(token)

        with self._lease():
            self._raise_if_stopped(token)
            remote_stat = self._smb.stat(item.temp_path)
            self._raise_if_stopped(token)
            if remote_stat is None or remote_stat.is_directory:
                remote_result = HashResult(_EMPTY_HASH, 0)
                after_remote_stat = remote_stat
            else:
                with self._smb.open_read(item.temp_path) as remote:
                    remote.seek(0)
                    callback = self._progress
                    def report_progress(offset: int) -> None:
                        self._raise_if_stopped(token)
                        if callback is not None:
                            callback(item, offset)
                        self._raise_if_stopped(token)

                    remote_result = sha256_stream(remote, progress=report_progress)
                self._raise_if_stopped(token)
                after_remote_stat = self._smb.stat(item.temp_path)

        self._raise_if_stopped(token)
        after = self._local.fingerprint(item.source_path)
        source_unchanged = before == expected and after == expected
        remote_exists = after_remote_stat is not None and not after_remote_stat.is_directory
        stable_remote_identity = (
            remote_stat is not None
            and after_remote_stat is not None
            and remote_stat.file_id == after_remote_stat.file_id
            and (remote_stat.file_id is not None or after_remote_stat.file_id is None)
        )
        identity_changed = (
            remote_stat is not None
            and after_remote_stat is not None
            and remote_stat.file_id != after_remote_stat.file_id
        )
        matches = (
            source_unchanged
            and remote_exists
            and not identity_changed
            and source_result.byte_count == expected.size
            and remote_result.byte_count == expected.size
            and after_remote_stat is not None
            and after_remote_stat.size == remote_result.byte_count
            and after_remote_stat.size == expected.size
            and source_result.byte_count == remote_result.byte_count
            and source_result.hexdigest == remote_result.hexdigest
        )
        return VerificationResult(
            matches=matches,
            source_unchanged=source_unchanged,
            source_hash=source_result.hexdigest,
            remote_hash=remote_result.hexdigest,
            source_bytes=source_result.byte_count,
            remote_bytes=remote_result.byte_count,
            session_generation=self._session.session_generation,
            remote_file_id=(
                remote_stat.file_id
                if stable_remote_identity and remote_stat is not None
                else None
            ),
        )

    def _verify_empty_directory(
        self,
        item: TransferItemRecord,
        token: object | None,
    ) -> VerificationResult:
        expected = item.source_fingerprint
        self._raise_if_stopped(token)
        before = self._local.fingerprint(item.source_path)
        with self._lease():
            self._raise_if_stopped(token)
            remote_before = self._smb.stat(item.temp_path)
            self._raise_if_stopped(token)
            entries_before = (
                self._smb.list_dir(item.temp_path)
                if remote_before is not None and remote_before.is_directory
                else [RemoteEntry("invalid", False, 0)]
            )
            self._raise_if_stopped(token)
            remote_after = self._smb.stat(item.temp_path)
            self._raise_if_stopped(token)
            entries_after = (
                self._smb.list_dir(item.temp_path)
                if remote_after is not None and remote_after.is_directory
                else [RemoteEntry("invalid", False, 0)]
            )
        self._raise_if_stopped(token)
        after = self._local.fingerprint(item.source_path)
        stable_remote_identity = (
            remote_before is not None
            and remote_after is not None
            and remote_before.file_id == remote_after.file_id
        )
        matches = (
            before == expected
            and after == expected
            and remote_before is not None
            and remote_before.is_directory
            and remote_after is not None
            and remote_after.is_directory
            and stable_remote_identity
            and not entries_before
            and not entries_after
        )
        return VerificationResult(
            matches=matches,
            source_unchanged=before == expected and after == expected,
            source_hash=_EMPTY_HASH,
            remote_hash=_EMPTY_HASH,
            source_bytes=0,
            remote_bytes=0,
            session_generation=self._session.session_generation,
            remote_file_id=(
                remote_before.file_id
                if stable_remote_identity and remote_before is not None
                else None
            ),
        )

    @staticmethod
    def _raise_if_stopped(token: object | None) -> None:
        if token is None:
            return
        for name in ("cancel_requested", "pause_requested"):
            value = getattr(token, name, False)
            if callable(value):
                value = value()
            if type(value) is bool and value:
                raise InterruptedError("verification stopped")

    def _lease(self) -> AbstractContextManager[None]:
        generation = self._session.session_generation
        if not self._smb.is_generation_current(generation):
            raise OSError("SMB session generation is not current")
        return self._smb.session_lease(generation)


__all__ = ["IntegrityVerifier", "VerificationResult"]
