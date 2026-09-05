from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import BinaryIO

import pytest

from nasmove.core.model import (
    Checkpoint,
    RemotePath,
    SourceFingerprint,
    TaskId,
    TransferItemId,
    TransferItemRecord,
)
from nasmove.core.ports import RemoteStat, SessionInfo
from nasmove.core.states import ItemState, SourceKind


class TransferLocal:
    def __init__(self, content: bytes, trace: list[str]) -> None:
        self.content = content
        self.trace = trace
        self.remove_calls: list[Path] = []
        self.fingerprint_calls = 0
        self.remove_error: BaseException | None = None
        self.source_exists = True
        self.cancel_token: TransferToken | None = None
        self.cancel_on_eof = False
        self.fingerprint_override: SourceFingerprint | None = None

    @contextmanager
    def open_read(self, path: Path) -> Iterator[BinaryIO]:
        del path
        if not self.source_exists:
            raise FileNotFoundError("source is absent")
        yield _TracingLocalStream(self.content, self.trace, self.cancel_token, self.cancel_on_eof)

    def fingerprint(self, path: Path) -> SourceFingerprint:
        del path
        self.fingerprint_calls += 1
        if not self.source_exists:
            raise FileNotFoundError("source is absent")
        if self.fingerprint_override is not None:
            return self.fingerprint_override
        return SourceFingerprint(1, 2, SourceKind.FILE, len(self.content), 1)

    def remove_file(
        self, path: Path, expected_fingerprint: SourceFingerprint | None = None
    ) -> None:
        del expected_fingerprint
        self.remove_calls.append(path)
        if self.remove_error is not None:
            error = self.remove_error
            if isinstance(error, RuntimeError) and str(error) == "injected crash":
                self.remove_error = None
            raise error
        if not self.source_exists:
            raise FileNotFoundError("source is absent")
        self.source_exists = False

    def remove_empty_dir(
        self, path: Path, expected_fingerprint: SourceFingerprint | None = None
    ) -> None:
        self.remove_file(path, expected_fingerprint)

    def list_dir(self, path: Path) -> list[Path]:
        del path
        return []


class _TracingLocalStream(BytesIO):
    def __init__(
        self,
        content: bytes,
        trace: list[str],
        cancel_token: TransferToken | None,
        cancel_on_eof: bool,
    ) -> None:
        super().__init__(content)
        self._trace = trace
        self._cancel_token = cancel_token
        self._cancel_on_eof = cancel_on_eof
        self._read_count = 0
        self._hashing = False

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 2:
            self._hashing = True
        return super().seek(offset, whence)

    def read(self, size: int = -1) -> bytes:
        result = super().read(size)
        self._read_count += 1
        if result and self._cancel_token is not None and self._read_count == 1:
            self._cancel_token.cancel_requested = True
        if not result and self._cancel_token is not None and self._cancel_on_eof:
            self._cancel_token.cancel_requested = True
        if self._hashing and result:
            self._trace.append("local.hash")
            self._hashing = False
        return result


class TransferRemote:
    def __init__(self, trace: list[str], *, content: bytes = b"") -> None:
        self.trace = trace
        self.files: dict[str, bytearray] = {}
        self.content = content
        self.path: RemotePath | None = None
        self.fail_write = False
        self.fail_flush = False
        self.fail_flush_after_write = False
        self.fail_stat = False
        self.short_write = False
        self.active_generation = 1
        self._lifecycle_lock = RLock()
        self._lease_owner: int | None = None
        self.generation_switch_blocked = False
        self.pending: dict[str, bytearray] = {}
        self.switch_generation_after_flush = False
        self.switch_generation_after_stat = False
        self.fail_stat_after_flush = False
        self.remote_read_started_at: int | None = None
        self.file_ids: dict[str, str | None] = {}
        self.next_file_id = 1
        self.replace_after_read = False
        self.replace_after_rename = False
        self.replacement_without_file_id = False
        self.replacement_content = b"corrupted payload"
        self.crash_before_rename = False
        self.crash_after_rename = False

    @contextmanager
    def create_exclusive(self, path: RemotePath) -> Iterator[BinaryIO]:
        self.trace.append(f"remote.create@{path.value}")
        if path.value in self.files or path.value in self.pending:
            raise FileExistsError(path.value)
        self.path = path
        self.files[path.value] = bytearray()
        self.file_ids[path.value] = f"file-{self.next_file_id}"
        self.next_file_id += 1
        self.pending[path.value] = bytearray()
        stream = _TracingRemoteStream(self, path)
        try:
            yield stream
        finally:
            stream.close()

    @contextmanager
    def open_update(self, path: RemotePath) -> Iterator[BinaryIO]:
        self.trace.append(f"remote.open_update@{path.value}")
        self.path = path
        if path.value not in self.files:
            raise FileNotFoundError(path.value)
        stream = _TracingRemoteStream(self, path)
        try:
            yield stream
        finally:
            stream.close()

    def stat(self, path: RemotePath) -> RemoteStat | None:
        if self.fail_stat:
            raise OSError("stat failure")
        value = self.files.get(path.value)
        if value is None:
            return None
        if self.fail_stat_after_flush and value:
            raise OSError("stat failure after flush")
        self.trace.append(f"remote.stat@{len(value)}")
        if path.value not in self.file_ids:
            self.file_ids[path.value] = f"file-{self.next_file_id}"
            self.next_file_id += 1
        result = RemoteStat(len(value), False, 0, self.file_ids[path.value])
        if self.switch_generation_after_stat:
            self.active_generation += 1
            self.switch_generation_after_stat = False
        return result

    @contextmanager
    def open_read(self, path: RemotePath) -> Iterator[BinaryIO]:
        self.trace.append(f"remote.open_read@{path.value}")
        value = self.files.get(path.value)
        if value is None:
            raise FileNotFoundError(path.value)
        stream = BytesIO(bytes(value))
        self.remote_read_started_at = stream.tell()
        try:
            yield stream
        finally:
            if self.replace_after_read:
                self.files[path.value] = bytearray(self.replacement_content)
                if self.replacement_without_file_id:
                    self.file_ids[path.value] = None
                else:
                    self.file_ids[path.value] = f"file-{self.next_file_id}"
                    self.next_file_id += 1

    def truncate(self, path: RemotePath, size: int) -> None:
        self.trace.append(f"remote.truncate@{size}")
        if path.value not in self.files:
            raise FileNotFoundError(path.value)
        self.files[path.value] = self.files[path.value][:size]

    def rename_exclusive(self, source: RemotePath, target: RemotePath) -> None:
        self.trace.append(f"remote.rename@{target.value}")
        if self.crash_before_rename:
            raise RuntimeError("injected crash before rename")
        if target.value in self.files:
            raise FileExistsError(target.value)
        if source.value not in self.files:
            raise FileNotFoundError(source.value)
        self.files[target.value] = self.files.pop(source.value)
        source_file_id = self.file_ids.pop(source.value, None)
        if source_file_id is not None:
            self.file_ids[target.value] = source_file_id
        if self.replace_after_rename:
            self.files[target.value] = bytearray(self.replacement_content)
            self.file_ids[target.value] = f"file-{self.next_file_id}"
            self.next_file_id += 1
        if self.crash_after_rename:
            raise RuntimeError("injected crash after rename")

    def list_dir(self, path: RemotePath) -> list[object]:
        prefix = path.value + "/"
        return [
            type("Entry", (), {"name": name[len(prefix) :], "is_directory": False, "size": len(value)})
            for name, value in self.files.items()
            if name.startswith(prefix) and "/" not in name[len(prefix) :]
        ]

    def is_generation_current(self, generation: int) -> bool:
        with self._lifecycle_lock:
            return self.active_generation == generation

    @contextmanager
    def session_lease(self, generation: int) -> Iterator[None]:
        with self._lifecycle_lock:
            if not self.is_generation_current(generation):
                raise OSError("stale session")
            self._lease_owner = 1
            try:
                yield
            finally:
                self._lease_owner = None

    def request_generation_switch(self) -> bool:
        acquired = self._lifecycle_lock.acquire(blocking=False)
        if not acquired:
            self.generation_switch_blocked = True
            return False
        try:
            if self._lease_owner is not None:
                self.generation_switch_blocked = True
                return False
            self.active_generation += 1
            return True
        finally:
            self._lifecycle_lock.release()

    def flush(self, stream: BinaryIO, offset: int) -> None:
        del stream
        self.trace.append(f"remote.flush@{offset}")


class _TracingRemoteStream(BytesIO):
    def __init__(self, remote: TransferRemote, path: RemotePath) -> None:
        self._remote = remote
        self._path = path
        super().__init__(bytes(remote.files.get(path.value, b"")))

    def write(self, data: bytes | bytearray) -> int:
        if self._remote.fail_write:
            raise OSError("write failure")
        payload = data[: 123 if self._remote.short_write else len(data)]
        written = super().write(payload)
        self._remote.pending[self._path.value] = bytearray(self.getvalue())
        return written

    def seek(self, offset: int, whence: int = 0) -> int:
        return super().seek(offset, whence)

    def flush(self) -> None:
        if self._remote.fail_flush:
            raise OSError("flush failure")
        self._remote.files[self._path.value] = bytearray(self.getvalue())
        self._remote.pending[self._path.value] = bytearray(self.getvalue())
        self._remote.trace.append(f"remote.flush@{len(self.getvalue())}")
        super().flush()
        if self._remote.switch_generation_after_flush:
            self._remote.active_generation += 1
            self._remote.switch_generation_after_flush = False
        if self._remote.fail_flush_after_write:
            raise OSError("flush result unknown")


class TransferRepository:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.checkpoints: list[Checkpoint] = []
        self.items: dict[TransferItemId, TransferItemRecord] = {}
        self.fail_done_transition_once = False

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        offset = checkpoint.confirmed_offset
        self.trace.append(f"repository.save_checkpoint@{offset}")
        self.checkpoints.append(checkpoint)

    def get_item(self, item_id: TransferItemId) -> TransferItemRecord:
        return self.items[item_id]

    def checkpoints_desc(self, item_id: TransferItemId) -> list[Checkpoint]:
        return sorted(
            (checkpoint for checkpoint in self.checkpoints if checkpoint.item_id == item_id),
            key=lambda checkpoint: (checkpoint.created_at, checkpoint.confirmed_offset),
            reverse=True,
        )

    def update_item_metadata(self, item: TransferItemRecord, expected_revision: int) -> None:
        current = self.items[item.id]
        if current.revision != expected_revision or current.state != item.state:
            raise RuntimeError("stale item revision")
        self.items[item.id] = replace(item, revision=expected_revision + 1)
        self.trace.append(f"repository.update_item@{item.final_path.value}")

    def transition_item(self, item_id: TransferItemId, expected: ItemState, target: ItemState) -> None:
        if target is ItemState.DONE and self.fail_done_transition_once:
            self.fail_done_transition_once = False
            raise RuntimeError("injected crash")
        current = self.items[item_id]
        if current.state is not expected:
            raise RuntimeError("unexpected item state")
        self.items[item_id] = replace(current, state=target, revision=current.revision + 1)
        self.trace.append(f"repository.transition_item@{target.value}")


class TransferToken:
    def __init__(self, pause: bool = False) -> None:
        self.pause_requested = pause
        self.cancel_requested = False


@dataclass
class FakeDependencies:
    local: TransferLocal
    remote: TransferRemote
    repository: TransferRepository
    trace: list[str]
    cancellation_token: TransferToken

    def as_kwargs(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "local_gateway": self.local,
            "smb_gateway": self.remote,
            "cancellation_token": self.cancellation_token,
        }

    def item(
        self,
        *,
        size: int | None = None,
        confirmed_offset: int = 0,
    ) -> TransferItemRecord:
        if size is None:
            size = len(self.local.content)
        content = self.local.content[:size]
        self.local.content = content
        return TransferItemRecord(
            id=TransferItemId("item-1"),
            task_id=TaskId("task-1"),
            source_path=Path("/source/file.bin"),
            relative_path=PurePosixPath("file.bin"),
            final_path=RemotePath("target/file.bin"),
            temp_path=RemotePath("target/.file.bin.part"),
            source_fingerprint=SourceFingerprint(1, 2, SourceKind.FILE, size, 1),
            state=ItemState.TRANSFERRING,
            confirmed_offset=confirmed_offset,
        )

    def session(self, *, generation: int) -> SessionInfo:
        self.remote.active_generation = generation
        return SessionInfo("3.1.1", True, True, generation)


@dataclass
class RecoveryFixture:
    local: TransferLocal
    remote: TransferRemote
    repository: TransferRepository
    coordinator: object
    item: TransferItemRecord
    item_id: TransferItemId

    @property
    def remote_size(self) -> int:
        return len(self.remote.files.get(self.item.temp_path.value, b""))

    @remote_size.setter
    def remote_size(self, value: int) -> None:
        self.remote.files[self.item.temp_path.value] = bytearray(self.local.content[:value])

    @property
    def remote_actions(self) -> list[str]:
        return [entry for entry in self.remote.trace if entry.startswith("remote.")]

    @property
    def isolated_paths(self) -> list[str]:
        return [path for path in self.remote.files if ".orphan" in path]

    @property
    def local_changed(self) -> bool:
        return self.local.fingerprint_override is not None

    @local_changed.setter
    def local_changed(self, value: bool) -> None:
        self.local.fingerprint_override = (
            SourceFingerprint(1, 2, SourceKind.FILE, len(self.local.content), 2) if value else None
        )

    def set_window_match(self, *, offset: int, matches: bool) -> None:
        from nasmove.localio.hashing import sha256_range

        window_length = min(4 * 1024 * 1024, offset)
        window_start = offset - window_length
        with self.local.open_read(self.item.source_path) as source:
            digest = sha256_range(source, window_start, window_length)
        if not matches:
            digest = "0" * 64 if digest != "0" * 64 else "1" * 64
        self.repository.checkpoints.append(
            Checkpoint(
                item_id=self.item.id,
                confirmed_offset=offset,
                remote_size=offset,
                window_start=window_start,
                window_length=window_length,
                window_sha256=digest,
                session_generation=1,
            )
        )

    def install_final_file(self, *, correct: bool) -> None:
        value = self.local.content if correct else self.local.content[:-1] + b"x"
        self.remote.files[self.item.final_path.value] = bytearray(value)


@pytest.fixture
def recovery_fixture() -> RecoveryFixture:
    from nasmove.transfer.recovery import RecoveryCoordinator

    trace: list[str] = []
    content = bytes(range(256)) * (128 * 1024 * 1024 // 256 + 1)
    local = TransferLocal(content, trace)
    remote = TransferRemote(trace)
    repository = TransferRepository(trace)
    item = FakeDependencies(local, remote, repository, trace, TransferToken()).item(
        size=128 * 1024 * 1024,
        confirmed_offset=128 * 1024 * 1024,
    )
    repository.items[item.id] = item
    session = SessionInfo("3.1.1", True, True, 1)
    remote.active_generation = 1
    fixture = RecoveryFixture(
        local=local,
        remote=remote,
        repository=repository,
        coordinator=RecoveryCoordinator(repository, local, remote, session),
        item=item,
        item_id=item.id,
    )
    return fixture


@pytest.fixture
def fake_dependencies() -> FakeDependencies:
    trace: list[str] = []
    return FakeDependencies(
        local=TransferLocal(bytes(range(256)) * (70 * 1024 * 1024 // 256 + 1), trace),
        remote=TransferRemote(trace),
        repository=TransferRepository(trace),
        trace=trace,
        cancellation_token=TransferToken(),
    )


@dataclass
class VerificationFixture:
    local: TransferLocal
    remote: TransferRemote
    repository: TransferRepository
    verifier: object
    item: TransferItemRecord

    @property
    def source_bytes(self) -> bytes:
        return self.local.content

    @source_bytes.setter
    def source_bytes(self, value: bytes) -> None:
        self.local.content = value
        self.item = replace(
            self.item,
            source_fingerprint=SourceFingerprint(1, 2, SourceKind.FILE, len(value), 1),
        )
        self.repository.items[self.item.id] = self.item

    @property
    def remote_bytes(self) -> bytes:
        return bytes(self.remote.files.get(self.item.temp_path.value, b""))

    @remote_bytes.setter
    def remote_bytes(self, value: bytes) -> None:
        self.remote.files[self.item.temp_path.value] = bytearray(value)
        self.remote.file_ids.setdefault(self.item.temp_path.value, "file-verification")


@pytest.fixture
def verification_fixture() -> VerificationFixture:
    from nasmove.transfer.verification import IntegrityVerifier

    trace: list[str] = []
    content = b"verified payload"
    local = TransferLocal(content, trace)
    remote = TransferRemote(trace)
    remote.files["target/.file.bin.part"] = bytearray(content)
    remote.file_ids["target/.file.bin.part"] = "file-verification"
    repository = TransferRepository(trace)
    item = FakeDependencies(local, remote, repository, trace, TransferToken()).item(size=len(content))
    repository.items[item.id] = item
    return VerificationFixture(
        local=local,
        remote=remote,
        repository=repository,
        verifier=IntegrityVerifier(repository, local, remote, session_generation=1),
        item=item,
    )


@dataclass
class CommitFixture:
    local: TransferLocal
    remote: TransferRemote
    repository: TransferRepository
    committer: object
    item: TransferItemRecord

    def occupy(self, name: str) -> None:
        self.remote.files[f"target/{name}"] = bytearray(self.local.content)

    def valid_verification(self) -> object:
        from nasmove.localio.hashing import sha256_stream
        from nasmove.transfer.verification import VerificationResult

        with self.local.open_read(self.item.source_path) as source:
            digest = sha256_stream(source)
        return VerificationResult(
            matches=True,
            source_unchanged=True,
            source_hash=digest.hexdigest,
            remote_hash=digest.hexdigest,
            source_bytes=digest.byte_count,
            remote_bytes=digest.byte_count,
            session_generation=1,
            remote_file_id=self.remote.file_ids.get(self.item.temp_path.value),
        )


@pytest.fixture
def commit_fixture() -> CommitFixture:
    from nasmove.transfer.commit import TargetCommitter

    trace: list[str] = []
    content = b"movie payload"
    local = TransferLocal(content, trace)
    remote = TransferRemote(trace)
    remote.files["target/.file.bin.part"] = bytearray(content)
    remote.file_ids["target/.file.bin.part"] = "file-commit"
    repository = TransferRepository(trace)
    item = FakeDependencies(local, remote, repository, trace, TransferToken()).item(size=len(content))
    item = replace(item, final_path=RemotePath("target/movie.mov"), state=ItemState.VERIFIED)
    repository.items[item.id] = item
    return CommitFixture(
        local=local,
        remote=remote,
        repository=repository,
        committer=TargetCommitter(repository, remote, session_generation=1),
        item=item,
    )


@dataclass
class DeletionVerifier:
    full_verify_calls: int = 0
    result: object | None = None
    source_hash: str = "0" * 64
    remote_hash: str = "0" * 64
    source_bytes: int = 0
    remote_bytes: int = 0
    session_generation: int = 1

    def complete_result(self) -> object:
        from nasmove.transfer.verification import VerificationResult

        return VerificationResult(
            matches=True,
            source_unchanged=True,
            source_hash=self.source_hash,
            remote_hash=self.remote_hash,
            source_bytes=self.source_bytes,
            remote_bytes=self.remote_bytes,
            session_generation=self.session_generation,
            remote_file_id="file-final",
        )

    def verify_full(self, item: TransferItemRecord) -> object:
        del item
        self.full_verify_calls += 1
        if self.result is None:
            return self.complete_result()
        return self.result


@dataclass
class DeletionFixture:
    local: TransferLocal
    remote: TransferRemote
    repository: TransferRepository
    verifier: DeletionVerifier
    service: object
    item: TransferItemRecord
    session: SessionInfo

    def replace_item(self, **changes: object) -> None:
        self.item = replace(self.repository.get_item(self.item.id), **changes)
        self.repository.items[self.item.id] = self.item

    def replace_session(self, *, generation: int) -> None:
        self.session = replace(self.session, session_generation=generation)
        self.remote.active_generation = generation
        self.verifier.session_generation = generation


@pytest.fixture
def deletion_fixture() -> DeletionFixture:
    from nasmove.localio.hashing import sha256_stream
    from nasmove.transfer.deletion import SourceDeletionService

    trace: list[str] = []
    content = b"verified source"
    local = TransferLocal(content, trace)
    remote = TransferRemote(trace)
    repository = TransferRepository(trace)
    item = FakeDependencies(local, remote, repository, trace, TransferToken()).item(size=len(content))
    with local.open_read(item.source_path) as source:
        digest = sha256_stream(source).hexdigest
    item = replace(
        item,
        state=ItemState.COMMITTED,
        sha256=digest,
        full_hash_verified=True,
        target_file_id="file-final",
        final_size=len(content),
        verified_session_generation=1,
    )
    remote.files[item.final_path.value] = bytearray(content)
    remote.file_ids[item.final_path.value] = "file-final"
    repository.items[item.id] = item
    verifier = DeletionVerifier(
        source_hash=digest,
        remote_hash=digest,
        source_bytes=len(content),
        remote_bytes=len(content),
    )
    session = SessionInfo("3.1.1", True, True, 1)
    service = SourceDeletionService(repository, local, remote, verifier)
    return DeletionFixture(local, remote, repository, verifier, service, item, session)
