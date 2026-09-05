from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from errno import ELOOP, ENOENT, ENOTDIR
from pathlib import Path, PurePosixPath
from typing import Protocol

from nasmove.core.errors import DomainValidationError
from nasmove.core.model import (
    ConnectionConfig,
    RemotePath,
    SourceFingerprint,
    TaskId,
    TaskRecord,
    TransferItemId,
    TransferItemRecord,
)
from nasmove.core.ports import SmbGateway
from nasmove.core.states import (
    ConflictPolicy,
    ItemState,
    SourceKind,
    TaskState,
    TransferAction,
    VerificationPolicy,
)
from nasmove.planning.conflicts import allocate_name
from nasmove.planning.paths import normalize_remote_path


class LocalFileGateway(Protocol):
    def fingerprint(self, path: Path) -> SourceFingerprint: ...


class PlanRepository(Protocol):
    """Atomically persist one task and its bounded item batches."""

    def persist_plan(
        self,
        task: TaskRecord,
        batches: Iterable[Sequence[TransferItemRecord]],
    ) -> None: ...


@dataclass(frozen=True, slots=True, init=False)
class PlanRequest:
    name: str
    connection: ConnectionConfig
    sources: tuple[Path, ...]
    target_root: RemotePath
    action: TransferAction
    conflict_policy: ConflictPolicy
    verification_policy: VerificationPolicy
    queue_position: int
    task_id: TaskId | None

    def __init__(
        self,
        name: str,
        connection: ConnectionConfig,
        sources: Sequence[Path] | None = None,
        target_root: RemotePath | None = None,
        action: TransferAction = TransferAction.COPY,
        conflict_policy: ConflictPolicy = ConflictPolicy.AUTO_RENAME,
        verification_policy: VerificationPolicy = VerificationPolicy.FULL,
        queue_position: int = 0,
        task_id: TaskId | None = None,
        *,
        source_paths: Sequence[Path] | None = None,
    ) -> None:
        if sources is None:
            sources = source_paths
        if not sources:
            raise DomainValidationError("at least one source path is required")
        if target_root is None:
            raise DomainValidationError("target_root is required")
        if queue_position < 0:
            raise DomainValidationError("queue_position must not be negative")
        if sources is not None and source_paths is not None and tuple(sources) != tuple(source_paths):
            raise DomainValidationError("sources and source_paths must agree")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "connection", connection)
        object.__setattr__(self, "sources", tuple(Path(path) for path in sources))
        object.__setattr__(self, "target_root", normalize_remote_path(target_root.value))
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "conflict_policy", conflict_policy)
        object.__setattr__(self, "verification_policy", verification_policy)
        object.__setattr__(self, "queue_position", queue_position)
        object.__setattr__(self, "task_id", task_id)

    @property
    def source_paths(self) -> tuple[Path, ...]:
        return self.sources


@dataclass(frozen=True, slots=True)
class PlannedTask:
    task: TaskRecord
    item_count: int
    total_bytes: int
    safety_margin: int
    required_space: int
    free_space: int

    @property
    def task_record(self) -> TaskRecord:
        return self.task


@dataclass(frozen=True, slots=True)
class _Discovered:
    path: Path
    relative_path: PurePosixPath
    fingerprint: SourceFingerprint
    state: ItemState


class TaskPlanner:
    BATCH_SIZE = 1_000
    SAFETY_MINIMUM = 1 << 30

    def __init__(
        self,
        local_gateway: LocalFileGateway,
        smb_gateway: SmbGateway,
        repository: PlanRepository | None = None,
    ) -> None:
        self._local = local_gateway
        self._smb = smb_gateway
        self._repository = repository

    def plan(self, request: PlanRequest) -> PlannedTask:
        sources = self._validate_sources(request.sources)
        total_bytes, total_files, item_count = self._summarize(sources)
        task_id = request.task_id or TaskId(str(uuid.uuid4()))
        safety_margin = max(self.SAFETY_MINIMUM, (total_bytes * 5 + 99) // 100)
        required_space = total_bytes + safety_margin
        free_space = self._smb.free_space(request.target_root)
        if free_space < required_space:
            raise DomainValidationError("insufficient NAS free space for planned transfer")
        now = datetime.now(UTC)
        task = TaskRecord(
            id=task_id,
            name=request.name,
            action=request.action,
            connection=request.connection,
            target_root=request.target_root,
            conflict_policy=request.conflict_policy,
            verification_policy=request.verification_policy,
            state=TaskState.PREFLIGHT,
            queue_position=request.queue_position,
            recovery_generation=1,
            total_files=total_files,
            total_bytes=total_bytes,
            copied_bytes=0,
            verified_bytes=0,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        if self._repository is not None:
            self._repository.persist_plan(task, self._iter_batches(request, sources, task_id))
        return PlannedTask(task, item_count, total_bytes, safety_margin, required_space, free_space)

    def _summarize(self, sources: Sequence[Path]) -> tuple[int, int, int]:
        total_bytes = 0
        total_files = 0
        item_count = 0
        for source in sources:
            for discovered in self._scan(source):
                item_count += 1
                if discovered.state is ItemState.PLANNED and discovered.fingerprint.kind is SourceKind.FILE:
                    total_bytes += discovered.fingerprint.size
                    total_files += 1
        return total_bytes, total_files, item_count

    def _iter_batches(
        self,
        request: PlanRequest,
        sources: Sequence[Path],
        task_id: TaskId,
    ) -> Iterator[tuple[TransferItemRecord, ...]]:
        occupied: dict[str, set[str]] = {}
        mapped: dict[tuple[str, ...], str] = {}
        batch: list[TransferItemRecord] = []
        for source in sources:
            for discovered in self._scan(source):
                batch.append(self._make_item(request, discovered, occupied, mapped, task_id))
                if len(batch) == self.BATCH_SIZE:
                    yield tuple(batch)
                    batch.clear()
        if batch:
            yield tuple(batch)

    @staticmethod
    def _validate_sources(sources: Sequence[Path]) -> tuple[Path, ...]:
        normalized = tuple(Path(os.path.abspath(source)) for source in sources)
        for index, source in enumerate(normalized):
            for other in normalized[index + 1 :]:
                if source == other or source in other.parents or other in source.parents:
                    raise DomainValidationError("source paths must not duplicate or overlap")
        return normalized

    def _scan(self, root: Path) -> Iterator[_Discovered]:
        info = root.lstat()
        if stat.S_ISLNK(info.st_mode):
            yield _Discovered(root, PurePosixPath(root.name), self._synthetic_fingerprint(info), ItemState.SKIPPED)
            return
        if stat.S_ISREG(info.st_mode):
            yield _Discovered(root, PurePosixPath(root.name), self._local.fingerprint(root), ItemState.PLANNED)
            return
        if not stat.S_ISDIR(info.st_mode):
            yield _Discovered(root, PurePosixPath(root.name), self._synthetic_fingerprint(info), ItemState.SKIPPED)
            return

        yield from self._scan_directory(root, PurePosixPath(root.name))

    def _scan_directory(self, directory: Path, relative: PurePosixPath) -> Iterator[_Discovered]:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(directory, flags)
        except OSError as exc:
            if exc.errno not in {ELOOP, ENOTDIR}:
                raise
            info = directory.lstat()
            yield _Discovered(directory, relative, self._synthetic_fingerprint(info), ItemState.SKIPPED)
            return
        try:
            yield from self._scan_directory_fd(directory_fd, directory, relative, flags)
        finally:
            os.close(directory_fd)

    def _scan_directory_fd(
        self,
        directory_fd: int,
        directory: Path,
        relative: PurePosixPath,
        flags: int,
    ) -> Iterator[_Discovered]:
        with os.scandir(directory_fd) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
            if not entries:
                info = os.fstat(directory_fd)
                yield _Discovered(
                    directory,
                    relative,
                    SourceFingerprint(info.st_dev, info.st_ino, SourceKind.EMPTY_DIRECTORY, 0, info.st_mtime_ns),
                    ItemState.PLANNED,
                )
                return
            for entry in entries:
                path = directory / entry.name
                child_relative = relative / entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    yield _Discovered(path, child_relative, self._synthetic_fingerprint(info), ItemState.SKIPPED)
                elif stat.S_ISREG(info.st_mode):
                    yield _Discovered(path, child_relative, self._local.fingerprint(path), ItemState.PLANNED)
                elif stat.S_ISDIR(info.st_mode):
                    try:
                        child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                    except OSError as exc:
                        if exc.errno not in {ELOOP, ENOTDIR}:
                            raise
                        yield _Discovered(path, child_relative, self._synthetic_fingerprint(info), ItemState.SKIPPED)
                    else:
                        try:
                            yield from self._scan_directory_fd(child_fd, path, child_relative, flags)
                        finally:
                            os.close(child_fd)
                else:
                    yield _Discovered(path, child_relative, self._synthetic_fingerprint(info), ItemState.SKIPPED)

    def _make_item(
        self,
        request: PlanRequest,
        discovered: _Discovered,
        occupied: dict[str, set[str]],
        mapped: dict[tuple[str, ...], str],
        task_id: TaskId,
    ) -> TransferItemRecord:
        relative_parts = tuple(discovered.relative_path.parts)
        target_parts: list[str] = []
        for index, source_name in enumerate(relative_parts):
            source_prefix = relative_parts[: index + 1]
            parent = request.target_root.value
            if target_parts:
                parent = f"{parent}/{('/'.join(target_parts))}"
            key = (str(discovered.path.parents[len(relative_parts) - index - 1]) if index == 0 else "", *source_prefix)
            if key not in mapped:
                names = occupied.setdefault(parent, self._remote_names(RemotePath(parent)))
                allocated = allocate_name(source_name, names)
                names.add(allocated)
                mapped[key] = allocated
            target_parts.append(mapped[key])
        final_value = f"{request.target_root.value}/{('/'.join(target_parts))}"
        final_path = normalize_remote_path(final_value)
        item_id = TransferItemId(str(uuid.uuid4()))
        temp_path = normalize_remote_path(
            f"{request.target_root.value}/{('/'.join(target_parts[:-1] + [f'.nasmove-{item_id}.part']))}"
        )
        return TransferItemRecord(
            id=item_id,
            task_id=task_id,
            source_path=discovered.path,
            relative_path=discovered.relative_path,
            final_path=final_path,
            temp_path=temp_path,
            source_fingerprint=discovered.fingerprint,
            state=discovered.state,
        )

    def _remote_names(self, path: RemotePath) -> set[str]:
        try:
            return {entry.name for entry in self._smb.list_dir(path)}
        except OSError as exc:
            if exc.errno == ENOENT:
                return set()
            raise

    @staticmethod
    def _synthetic_fingerprint(info: os.stat_result) -> SourceFingerprint:
        return SourceFingerprint(info.st_dev, info.st_ino, SourceKind.FILE, info.st_size, info.st_mtime_ns)
