from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from errno import ELOOP, ENOENT, ENOTDIR
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Protocol

from nasmove.core.errors import (
    ConflictResolutionRequired,
    DomainValidationError,
    PreflightCancelled,
)
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
    conflict_count: int = 0

    @property
    def task_record(self) -> TaskRecord:
        return self.task


@dataclass(frozen=True, slots=True)
class PreflightProgress:
    item_count: int
    total_files: int
    total_bytes: int
    current_path: Path


class PreflightCancellation:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class PreflightSession:
    def __init__(
        self,
        *,
        request: PlanRequest,
        summary: PlannedTask,
        spool_directory: tempfile.TemporaryDirectory[str],
        spool_path: Path,
        owner_token: object,
    ) -> None:
        self.request = request
        self.summary = summary
        self.spool_path = spool_path
        self._spool_directory = spool_directory
        self._owner_token = owner_token
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._spool_directory.cleanup()


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
        self._owner_token = object()

    def plan(self, request: PlanRequest) -> PlannedTask:
        session = self.preflight(request)
        if self._repository is None:
            summary = session.summary
            session.close()
            return summary
        return self.confirm_preflight(session)

    def preflight(
        self,
        request: PlanRequest,
        *,
        cancellation: PreflightCancellation | None = None,
        on_progress: Callable[[PreflightProgress], None] | None = None,
    ) -> PreflightSession:
        cancellation = cancellation or PreflightCancellation()
        self._raise_if_cancelled(cancellation)
        sources = self._validate_sources(request.sources)
        task_id = request.task_id or TaskId(str(uuid.uuid4()))
        spool_directory = tempfile.TemporaryDirectory(prefix="nasmove-preflight-")
        spool_path = Path(spool_directory.name) / "items.sqlite3"
        database = sqlite3.connect(spool_path)
        completed = False
        try:
            self._create_spool(database)
            _scanned_bytes, _scanned_files, item_count = self._scan_to_spool(
                database,
                sources,
                cancellation=cancellation,
                on_progress=on_progress,
            )
            conflict_count = self._plan_spooled_items(
                database, request, task_id, cancellation
            )
            total_bytes, total_files = self._planned_totals(database)
            safety_margin = max(self.SAFETY_MINIMUM, (total_bytes * 5 + 99) // 100)
            required_space = total_bytes + safety_margin
            self._raise_if_cancelled(cancellation)
            free_space = self._smb.free_space(request.target_root)
            self._raise_if_cancelled(cancellation)
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
            summary = PlannedTask(
                task,
                item_count,
                total_bytes,
                safety_margin,
                required_space,
                free_space,
                conflict_count,
            )
            completed = True
            return PreflightSession(
                request=request,
                summary=summary,
                spool_directory=spool_directory,
                spool_path=spool_path,
                owner_token=self._owner_token,
            )
        finally:
            database.close()
            if not completed:
                spool_directory.cleanup()

    def confirm_preflight(self, session: PreflightSession) -> PlannedTask:
        self._validate_session(session)
        if self._repository is None:
            session.close()
            raise DomainValidationError("task repository is required to confirm preflight")
        database = sqlite3.connect(session.spool_path)
        try:
            items = _TrackedItems(
                self._iter_spooled_items(database, session.summary.task.id)
            )
            self._repository.create_task(session.summary.task, items)
            if not items.exhausted:
                raise DomainValidationError(
                    "task repository did not consume all planned items"
                )
            return session.summary
        finally:
            database.close()
            session.close()

    def cancel_preflight(self, session: PreflightSession) -> None:
        if session._owner_token is not self._owner_token:
            raise DomainValidationError("preflight session belongs to another planner")
        session.close()

    def _validate_session(self, session: PreflightSession) -> None:
        if session._owner_token is not self._owner_token:
            raise DomainValidationError("preflight session belongs to another planner")
        if session.closed:
            raise PreflightCancelled("preflight session is no longer available")

    @staticmethod
    def _raise_if_cancelled(cancellation: PreflightCancellation) -> None:
        if cancellation.cancelled:
            raise PreflightCancelled("preflight was cancelled")

    @staticmethod
    def _create_spool(database: sqlite3.Connection) -> None:
        database.executescript(
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
            );
            CREATE TABLE planned_items (
                sequence INTEGER PRIMARY KEY,
                item_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                final_path TEXT NOT NULL,
                temp_path TEXT NOT NULL,
                device INTEGER NOT NULL,
                inode INTEGER NOT NULL,
                kind TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                state TEXT NOT NULL
            );"""
        )
        database.commit()

    def _scan_to_spool(
        self,
        database: sqlite3.Connection,
        sources: Sequence[Path],
        *,
        cancellation: PreflightCancellation,
        on_progress: Callable[[PreflightProgress], None] | None,
    ) -> tuple[int, int, int]:
        total_bytes = 0
        total_files = 0
        item_count = 0
        for source in sources:
            self._raise_if_cancelled(cancellation)
            for discovered in self._scan(source):
                self._raise_if_cancelled(cancellation)
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
                if on_progress is not None:
                    on_progress(
                        PreflightProgress(
                            item_count=item_count,
                            total_files=total_files,
                            total_bytes=total_bytes,
                            current_path=discovered.path,
                        )
                    )
                self._raise_if_cancelled(cancellation)
        database.commit()
        return total_bytes, total_files, item_count

    def _plan_spooled_items(
        self,
        database: sqlite3.Connection,
        request: PlanRequest,
        task_id: TaskId,
        cancellation: PreflightCancellation,
    ) -> int:
        conflict_index = _DiskConflictIndex(database, self._smb)
        conflict_count = 0
        for sequence, discovered in self._iter_spooled_discoveries(database):
            self._raise_if_cancelled(cancellation)
            item, item_conflict_count = self._make_item(
                request, discovered, conflict_index, task_id
            )
            conflict_count += item_conflict_count
            database.execute(
                "INSERT INTO planned_items(sequence, item_id, source_path, relative_path, final_path, "
                "temp_path, device, inode, kind, size, mtime_ns, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sequence,
                    str(item.id),
                    str(item.source_path),
                    item.relative_path.as_posix(),
                    item.final_path.value,
                    item.temp_path.value,
                    item.source_fingerprint.device,
                    item.source_fingerprint.inode,
                    item.source_fingerprint.kind.value,
                    item.source_fingerprint.size,
                    item.source_fingerprint.mtime_ns,
                    item.state.value,
                ),
            )
        database.commit()
        return conflict_count

    @staticmethod
    def _planned_totals(database: sqlite3.Connection) -> tuple[int, int]:
        row = database.execute(
            "SELECT COALESCE(SUM(size), 0), COUNT(*) FROM planned_items "
            "WHERE state = ? AND kind = ?",
            (ItemState.PLANNED.value, SourceKind.FILE.value),
        ).fetchone()
        return int(row[0]), int(row[1])

    @staticmethod
    def _iter_spooled_discoveries(
        database: sqlite3.Connection,
    ) -> Iterator[tuple[int, _Discovered]]:
        rows = database.execute(
            "SELECT sequence, source_key, source_path, relative_path, device, inode, kind, size, "
            "mtime_ns, state FROM items ORDER BY source_key, relative_path, sequence"
        )
        for row in rows:
            yield row[0], _Discovered(
                source_key=row[1],
                path=Path(row[2]),
                relative_path=PurePosixPath(row[3]),
                fingerprint=SourceFingerprint(
                    device=row[4],
                    inode=row[5],
                    kind=SourceKind(row[6]),
                    size=row[7],
                    mtime_ns=row[8],
                ),
                state=ItemState(row[9]),
            )

    def _iter_spooled_items(
        self,
        database: sqlite3.Connection,
        task_id: TaskId,
    ) -> Iterator[TransferItemRecord]:
        rows = database.execute(
            "SELECT item_id, source_path, relative_path, final_path, temp_path, device, inode, kind, "
            "size, mtime_ns, state FROM planned_items ORDER BY sequence"
        )
        for row in rows:
            yield TransferItemRecord(
                id=TransferItemId(row[0]),
                task_id=task_id,
                source_path=Path(row[1]),
                relative_path=PurePosixPath(row[2]),
                final_path=RemotePath(row[3]),
                temp_path=RemotePath(row[4]),
                source_fingerprint=SourceFingerprint(
                    device=row[5],
                    inode=row[6],
                    kind=SourceKind(row[7]),
                    size=row[8],
                    mtime_ns=row[9],
                ),
                state=ItemState(row[10]),
            )

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
        source = TaskPlanner._canonicalize_macos_system_alias(source)
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

    @staticmethod
    def _canonicalize_macos_system_alias(source: Path) -> Path:
        """Map Apple's safe /var and /tmp aliases to their real prefixes.

        macOS exposes these two system paths as symlinks to /private.  They
        are safe platform aliases, unlike arbitrary user-controlled symlink
        ancestors which must remain rejected by the no-follow traversal.
        """
        if not source.is_absolute() or len(source.parts) < 2:
            return source
        alias = Path(source.anchor) / source.parts[1]
        if alias not in {Path("/var"), Path("/tmp")}:
            return source
        try:
            resolved = alias.resolve(strict=True)
        except OSError:
            return source
        expected = Path("/private") / source.parts[1]
        if resolved != expected:
            return source
        return resolved.joinpath(*source.parts[2:])

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
    ) -> tuple[TransferItemRecord, int]:
        relative_parts = tuple(discovered.relative_path.parts)
        target_parts: list[str] = []
        conflict_count = 0
        item_state = discovered.state
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

                collision = is_occupied(conflict_key(source_name))
                is_leaf = component_index == len(relative_parts) - 1
                if not collision:
                    mapped_name = source_name
                elif request.conflict_policy is ConflictPolicy.KEEP_BOTH:
                    mapped_name = allocate_name_with_index(source_name, is_occupied)
                elif not is_leaf:
                    mapped_name = source_name
                elif request.conflict_policy is ConflictPolicy.ASK:
                    raise ConflictResolutionRequired(
                        RemotePath(f"{parent}/{source_name}")
                    )
                else:
                    mapped_name = source_name
                    if request.conflict_policy is ConflictPolicy.SKIP:
                        item_state = ItemState.SKIPPED
                    elif request.conflict_policy is ConflictPolicy.OVERWRITE_IF_NEWER:
                        remote = self._smb.stat(RemotePath(f"{parent}/{source_name}"))
                        if (
                            remote is not None
                            and discovered.fingerprint.mtime_ns <= remote.modified_ns
                        ):
                            item_state = ItemState.SKIPPED
                if collision:
                    conflict_count += 1
                conflict_index.reserve(parent, mapped_name)
                conflict_index.save_mapping(discovered.source_key, prefix, mapped_name)
            target_parts.append(mapped_name)
        final_value = f"{request.target_root.value}/{('/'.join(target_parts))}"
        final_path = normalize_remote_path(final_value)
        item_id = TransferItemId(str(uuid.uuid4()))
        temp_path = normalize_remote_path(
            f"{request.target_root.value}/{('/'.join(target_parts[:-1] + [f'.nasmove-{item_id}.part']))}"
        )
        return (
            TransferItemRecord(
                id=item_id,
                task_id=task_id,
                source_path=discovered.path,
                relative_path=discovered.relative_path,
                final_path=final_path,
                temp_path=temp_path,
                source_fingerprint=discovered.fingerprint,
                state=item_state,
            ),
            conflict_count,
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
