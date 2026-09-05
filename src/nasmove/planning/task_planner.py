from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from errno import ENOENT
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

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
        object.__setattr__(self, "target_root", target_root)
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
    items: tuple[TransferItemRecord, ...]
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
        repository: Any | None = None,
        *,
        batch_sink: Callable[[Sequence[TransferItemRecord]], None] | None = None,
    ) -> None:
        self._local = local_gateway
        self._smb = smb_gateway
        self._repository = repository
        self._batch_sink = batch_sink

    def plan(self, request: PlanRequest) -> PlannedTask:
        occupied: dict[str, set[str]] = {}
        mapped: dict[tuple[str, ...], str] = {}
        items: list[TransferItemRecord] = []
        batch: list[TransferItemRecord] = []
        total_bytes = 0
        total_files = 0
        task_id = request.task_id or TaskId(str(uuid.uuid4()))

        for source in request.sources:
            absolute = Path(os.path.abspath(source))
            for discovered in self._scan(absolute):
                if discovered.state is ItemState.PLANNED and discovered.fingerprint.kind is SourceKind.FILE:
                    total_bytes += discovered.fingerprint.size
                    total_files += 1
                item = self._make_item(request, discovered, occupied, mapped, task_id)
                items.append(item)
                batch.append(item)
                if len(batch) == self.BATCH_SIZE:
                    self._save_batch(batch)
                    batch.clear()
        if batch:
            self._save_batch(batch)

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
        return PlannedTask(task, tuple(items), total_bytes, safety_margin, required_space, free_space)

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
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        if not entries:
            info = directory.lstat()
            yield _Discovered(
                directory,
                relative,
                SourceFingerprint(info.st_dev, info.st_ino, SourceKind.EMPTY_DIRECTORY, 0, info.st_mtime_ns),
                ItemState.PLANNED,
            )
            return
        for entry in entries:
            path = Path(entry.path)
            child_relative = relative / entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                yield _Discovered(path, child_relative, self._synthetic_fingerprint(info), ItemState.SKIPPED)
            elif stat.S_ISREG(info.st_mode):
                yield _Discovered(path, child_relative, self._local.fingerprint(path), ItemState.PLANNED)
            elif stat.S_ISDIR(info.st_mode):
                yield from self._scan_directory(path, child_relative)
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
        name = target_parts[-1]
        temp_path = normalize_remote_path(
            f"{request.target_root.value}/{('/'.join(target_parts[:-1] + [f'.{name}.part']))}"
        )
        return TransferItemRecord(
            id=TransferItemId(str(uuid.uuid4())),
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

    def _save_batch(self, batch: Sequence[TransferItemRecord]) -> None:
        frozen = tuple(batch)
        if self._batch_sink is not None:
            self._batch_sink(frozen)
        if self._repository is not None:
            for method_name in ("save_item_batch", "save_items_batch", "save_batch", "save_items", "append_items"):
                method = getattr(self._repository, method_name, None)
                if method is not None:
                    method(frozen)
                    break

    @staticmethod
    def _synthetic_fingerprint(info: os.stat_result) -> SourceFingerprint:
        return SourceFingerprint(info.st_dev, info.st_ino, SourceKind.FILE, info.st_size, info.st_mtime_ns)
