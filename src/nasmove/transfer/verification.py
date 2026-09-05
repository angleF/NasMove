from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from nasmove.core.model import RemotePath, SourceFingerprint, TransferItemRecord
from nasmove.core.ports import RemoteStat, SessionInfo
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


@dataclass(frozen=True, slots=True)
class VerificationResult:
    matches: bool
    source_unchanged: bool
    source_hash: str
    remote_hash: str
    source_bytes: int
    remote_bytes: int
    session_generation: int

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

    def verify_full(self, item: TransferItemRecord) -> VerificationResult:
        expected = item.source_fingerprint
        before = self._local.fingerprint(item.source_path)
        with self._local.open_read(item.source_path) as source:
            source.seek(0)
            source_result = sha256_stream(source)

        with self._lease():
            remote_stat = self._smb.stat(item.temp_path)
            if remote_stat is None or remote_stat.is_directory:
                remote_result = HashResult(_EMPTY_HASH, 0)
                after_remote_stat = remote_stat
            else:
                with self._smb.open_read(item.temp_path) as remote:
                    remote.seek(0)
                    remote_result = sha256_stream(remote)
                after_remote_stat = self._smb.stat(item.temp_path)

        after = self._local.fingerprint(item.source_path)
        source_unchanged = before == expected and after == expected
        remote_exists = after_remote_stat is not None and not after_remote_stat.is_directory
        matches = (
            source_unchanged
            and remote_exists
            and source_result.byte_count == expected.size
            and remote_result.byte_count == expected.size
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
        )

    def _lease(self) -> AbstractContextManager[None]:
        generation = self._session.session_generation
        if not self._smb.is_generation_current(generation):
            raise OSError("SMB session generation is not current")
        return self._smb.session_lease(generation)


__all__ = ["IntegrityVerifier", "VerificationResult"]
