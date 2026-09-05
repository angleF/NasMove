from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
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
from nasmove.planning.conflicts import allocate_name_with_index, conflict_key
from nasmove.planning.paths import normalize_remote_path


class LocalFileGateway(Protocol):
    def fingerprint(self, path: Path) -> SourceFingerprint: ...


class TaskRepository(Protocol):
    """Persist one task by fully consuming items in one atomic transaction.

    Implementations must consume the iterable completely, insert at most 1,000
    items per batch, and roll back the task and all items if iteration, insert,
    or commit raises. Returning early without consuming the iterable is invalid.
    """

    def create_task(
        self,
        task: TaskRecord,
        items: Iterable[TransferItemRecord],
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
    source_key: str
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
        repository: TaskRepository | None = None,
    ) -> None:
        self._local = local_gateway
        self._smb = smb_gateway
        self._repository = repository

    def plan(self, request: PlanRequest) -> PlannedTask:
        sources = self._validate_sources(request.sources)
        task_id = request.task_id or TaskId(str(uuid.uuid4()))
        with tempfile.TemporaryDirectory(prefix="nasmove-plan-") as spool_dir:
            database = sqlite3.connect(Path(spool_dir) / "items.sqlite3")
            try:
                self._create_spool(database)
                total_bytes, total_files, item_count = self._scan_to_spool(database, sources)
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
                    index = _DiskConflictIndex(database, self._smb)
                    items = _TrackedItems(self._iter_spooled_items(database, request, task_id, index))
                    self._repository.create_task(task, items)
                    if not items.exhausted:
                        raise DomainValidationError("task repository did not consume all planned items")
                return PlannedTask(task, item_count, total_bytes, safety_margin, required_space, free_space)
            finally:
                database.close()

    @staticmethod
    def _create_spool(database: sqlite3.Connection) -> None:
        database.execute(
            """CREATE TABLE items (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                source_path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                device INTEGER NOT NULL,
                inode INTEGER NOT NULL,
                kind TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                state TEXT NOT NULL
            )"""
        )
        database.commit()

    def _scan_to_spool(self, database: sqlite3.Connection, sources: Sequence[Path]) -> tuple[int, int, int]:
        total_bytes = 0
        total_files = 0
        item_count = 0
        for source in sources:
            for discovered in self._scan(source):
                database.execute(
                    "INSERT INTO items(source_key, source_path, relative_path, device, inode, kind, size, mtime_ns, state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        discovered.source_key,
                        str(discovered.path),
                        discovered.relative_path.as_posix(),
                        discovered.fingerprint.device,
                        discovered.fingerprint.inode,
                        discovered.fingerprint.kind.value,
                        discovered.fingerprint.size,
                        discovered.fingerprint.mtime_ns,
                        discovered.state.value,
                    ),
                )
                item_count += 1
                if discovered.state is ItemState.PLANNED and discovered.fingerprint.kind is SourceKind.FILE:
                    total_bytes += discovered.fingerprint.size
                    total_files += 1
        database.commit()
        return total_bytes, total_files, item_count

    def _iter_spooled_items(
        self,
        database: sqlite3.Connection,
        request: PlanRequest,
        task_id: TaskId,
        index: _DiskConflictIndex,
    ) -> Iterator[TransferItemRecord]:
        rows = database.execute(
            "SELECT source_key, source_path, relative_path, device, inode, kind, size, mtime_ns, state "
            "FROM items ORDER BY source_key, relative_path, sequence"
        )
        for row in rows:
            discovered = _Discovered(
                source_key=row[0],
                path=Path(row[1]),
                relative_path=PurePosixPath(row[2]),
                fingerprint=SourceFingerprint(
                    device=row[3],
                    inode=row[4],
                    kind=SourceKind(row[5]),
                    size=row[6],
                    mtime_ns=row[7],
                ),
                state=ItemState(row[8]),
            )
            yield self._make_item(request, discovered, index, task_id)

    @staticmethod
    def _validate_sources(sources: Sequence[Path]) -> tuple[Path, ...]:
        normalized = tuple(Path(os.path.abspath(source)) for source in sources)
        identities: dict[tuple[int, int], Path] = {}
        for index, source in enumerate(normalized):
            for other in normalized[index + 1 :]:
                if source == other or source in other.parents or other in source.parents:
                    raise DomainValidationError("source paths must not duplicate or overlap")
            identity = TaskPlanner._source_identity(source)
            previous = identities.setdefault(identity, source)
            if previous != source:
                raise DomainValidationError("source paths must not alias the same filesystem object")
        return normalized

    @staticmethod
    def _source_identity(source: Path) -> tuple[int, int]:
        parent_fd = TaskPlanner._open_parent(source)
        try:
            info = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
            return info.st_dev, info.st_ino
        except OSError as exc:
            if exc.errno in {ELOOP, ENOTDIR}:
                raise DomainValidationError("source path or an ancestor must not be a symlink") from exc
            raise
        finally:
            os.close(parent_fd)

    @staticmethod
    def _open_parent(source: Path) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(os.sep, flags)
        try:
            for component in source.parts[1:-1]:
                next_fd = os.open(component, flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            return parent_fd
        except OSError as exc:
            os.close(parent_fd)
            if exc.errno in {ELOOP, ENOTDIR}:
                raise DomainValidationError("source path or an ancestor must not be a symlink") from exc
            raise

    def _scan(self, root: Path) -> Iterator[_Discovered]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = self._open_parent(root)
        try:
            info = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                yield _Discovered(
                    str(root), root, PurePosixPath(root.name), self._synthetic_fingerprint(info), ItemState.SKIPPED
                )
            elif stat.S_ISREG(info.st_mode):
                final_fd = os.open(root.name, flags, dir_fd=parent_fd)
                try:
                    bound = os.fstat(final_fd)
                    if stat.S_ISREG(bound.st_mode):
                        yield _Discovered(
                            str(root), root, PurePosixPath(root.name), self._fingerprint(bound), ItemState.PLANNED
                        )
                    else:
                        yield _Discovered(
                            str(root), root, PurePosixPath(root.name), self._synthetic_fingerprint(bound), ItemState.SKIPPED
                        )
                finally:
                    os.close(final_fd)
            else:
                final_fd = os.open(root.name, flags | os.O_DIRECTORY, dir_fd=parent_fd)
                try:
                    bound = os.fstat(final_fd)
                    if stat.S_ISDIR(bound.st_mode):
                        yield from self._scan_directory_fd(final_fd, root, PurePosixPath(root.name), flags, str(root))
                    else:
                        yield _Discovered(
                            str(root), root, PurePosixPath(root.name), self._synthetic_fingerprint(bound), ItemState.SKIPPED
                        )
                finally:
                    os.close(final_fd)
        except OSError as exc:
            if exc.errno in {ELOOP, ENOTDIR}:
                raise DomainValidationError("source path or an ancestor must not be a symlink") from exc
            raise
        finally:
            os.close(parent_fd)

    def _scan_directory_fd(
        self,
        directory_fd: int,
        directory: Path,
        relative: PurePosixPath,
        flags: int,
        source_key: str,
    ) -> Iterator[_Discovered]:
        with os.scandir(directory_fd) as iterator:
            has_entries = False
            for entry in iterator:
                has_entries = True
                path = directory / entry.name
                child_relative = relative / entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    yield _Discovered(source_key, path, child_relative, self._synthetic_fingerprint(info), ItemState.SKIPPED)
                elif stat.S_ISREG(info.st_mode):
                    try:
                        file_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                    except OSError as exc:
                        if exc.errno not in {ELOOP, ENOTDIR}:
                            raise
                        yield _Discovered(source_key, path, child_relative, self._synthetic_fingerprint(info), ItemState.SKIPPED)
                    else:
                        try:
                            bound = os.fstat(file_fd)
                            if stat.S_ISREG(bound.st_mode):
                                yield _Discovered(source_key, path, child_relative, self._fingerprint(bound), ItemState.PLANNED)
                            else:
                                yield _Discovered(source_key, path, child_relative, self._synthetic_fingerprint(bound), ItemState.SKIPPED)
                        finally:
                            os.close(file_fd)
                elif stat.S_ISDIR(info.st_mode):
                    try:
                        child_fd = os.open(entry.name, flags | os.O_DIRECTORY, dir_fd=directory_fd)
                    except OSError as exc:
                        if exc.errno not in {ELOOP, ENOTDIR}:
                            raise
                        yield _Discovered(source_key, path, child_relative, self._synthetic_fingerprint(info), ItemState.SKIPPED)
                    else:
                        try:
                            yield from self._scan_directory_fd(child_fd, path, child_relative, flags, source_key)
                        finally:
                            os.close(child_fd)
                else:
                    yield _Discovered(source_key, path, child_relative, self._synthetic_fingerprint(info), ItemState.SKIPPED)
            if not has_entries:
                info = os.fstat(directory_fd)
                yield _Discovered(
                    source_key,
                    directory,
                    relative,
                    SourceFingerprint(info.st_dev, info.st_ino, SourceKind.EMPTY_DIRECTORY, 0, info.st_mtime_ns),
                    ItemState.PLANNED,
                )

    def _make_item(
        self,
        request: PlanRequest,
        discovered: _Discovered,
        conflict_index: _DiskConflictIndex,
        task_id: TaskId,
    ) -> TransferItemRecord:
        relative_parts = tuple(discovered.relative_path.parts)
        target_parts: list[str] = []
        for component_index, source_name in enumerate(relative_parts):
            source_prefix = relative_parts[: component_index + 1]
            parent = request.target_root.value
            if target_parts:
                parent = f"{parent}/{('/'.join(target_parts))}"
            prefix = "/".join(source_prefix)
            mapped_name = conflict_index.mapping(discovered.source_key, prefix)
            if mapped_name is None:
                conflict_index.ensure_parent(parent)
                def is_occupied(key: str, target_parent: str = parent) -> bool:
                    return conflict_index.occupied(target_parent, key)

                mapped_name = allocate_name_with_index(source_name, is_occupied)
                conflict_index.reserve(parent, mapped_name)
                conflict_index.save_mapping(discovered.source_key, prefix, mapped_name)
            target_parts.append(mapped_name)
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

    @staticmethod
    def _fingerprint(info: os.stat_result) -> SourceFingerprint:
        return SourceFingerprint(info.st_dev, info.st_ino, SourceKind.FILE, info.st_size, info.st_mtime_ns)

    _synthetic_fingerprint = _fingerprint


class _TrackedItems(Iterator[TransferItemRecord]):
    def __init__(self, items: Iterator[TransferItemRecord]) -> None:
        self._items = items
        self.exhausted = False

    def __iter__(self) -> _TrackedItems:
        return self

    def __next__(self) -> TransferItemRecord:
        try:
            return next(self._items)
        except StopIteration:
            self.exhausted = True
            raise


class _DiskConflictIndex:
    def __init__(self, database: sqlite3.Connection, smb_gateway: SmbGateway) -> None:
        self._database = database
        self._smb = smb_gateway
        database.executescript(
            """CREATE TABLE occupied(parent TEXT NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL,
                PRIMARY KEY(parent, key));
            CREATE TABLE listed_dirs(parent TEXT PRIMARY KEY);
            CREATE TABLE mappings(source_key TEXT NOT NULL, source_prefix TEXT NOT NULL, name TEXT NOT NULL,
                PRIMARY KEY(source_key, source_prefix));"""
        )
        database.commit()

    def ensure_parent(self, parent: str) -> None:
        found = self._database.execute("SELECT 1 FROM listed_dirs WHERE parent = ?", (parent,)).fetchone()
        if found is not None:
            return
        try:
            entries = self._smb.list_dir(RemotePath(parent))
        except OSError as exc:
            if exc.errno != ENOENT:
                raise
            entries = []
        for entry in entries:
            self._database.execute(
                "INSERT OR IGNORE INTO occupied(parent, key, name) VALUES (?, ?, ?)",
                (parent, conflict_key(entry.name), entry.name),
            )
        self._database.execute("INSERT INTO listed_dirs(parent) VALUES (?)", (parent,))

    def occupied(self, parent: str, key: str) -> bool:
        return self._database.execute(
            "SELECT 1 FROM occupied WHERE parent = ? AND key = ?", (parent, key)
        ).fetchone() is not None

    def reserve(self, parent: str, name: str) -> None:
        self._database.execute(
            "INSERT OR IGNORE INTO occupied(parent, key, name) VALUES (?, ?, ?)",
            (parent, conflict_key(name), name),
        )

    def mapping(self, source_key: str, source_prefix: str) -> str | None:
        row = self._database.execute(
            "SELECT name FROM mappings WHERE source_key = ? AND source_prefix = ?",
            (source_key, source_prefix),
        ).fetchone()
        return None if row is None else row[0]

    def save_mapping(self, source_key: str, source_prefix: str, name: str) -> None:
        self._database.execute(
            "INSERT INTO mappings(source_key, source_prefix, name) VALUES (?, ?, ?)",
            (source_key, source_prefix, name),
        )
