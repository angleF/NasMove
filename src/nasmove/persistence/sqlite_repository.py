from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import Lock, local
from typing import Self, cast

from nasmove.core.errors import ConcurrentStateChange, ConnectionProfileInUse
from nasmove.core.model import (
    Checkpoint,
    ConnectionConfig,
    ConnectionProfileId,
    RemotePath,
    SourceFingerprint,
    TaskId,
    TaskRecord,
    TransferItemId,
    TransferItemRecord,
)
from nasmove.core.states import (
    ConflictPolicy,
    ItemState,
    SourceKind,
    TaskState,
    TransferAction,
    VerificationPolicy,
)
from nasmove.core.transitions import assert_item_transition, assert_task_transition
from nasmove.persistence.schema import has_supported_schema, initialize_database

_TERMINAL_TASK_STATES = (
    TaskState.COMPLETED,
    TaskState.COMPLETED_WITH_WARNINGS,
    TaskState.FAILED,
    TaskState.CANCELED,
)
_ACTIVE_TASK_STATES = (
    TaskState.RUNNING,
    TaskState.VERIFYING,
    TaskState.COMMITTING,
    TaskState.DELETING_SOURCE,
)


def _encode_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _decode_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _bool_value(value: bool) -> int:
    return 1 if value else 0


class SqliteTaskRepository(AbstractContextManager["SqliteTaskRepository"]):
    """Durable TaskRepository implementation backed by SQLite."""

    BATCH_SIZE = 1_000
    _INITIAL_TASK_STATES = frozenset({TaskState.DRAFT, TaskState.PREFLIGHT, TaskState.QUEUED})
    _INITIAL_ITEM_STATES = frozenset({ItemState.PLANNED, ItemState.SKIPPED})

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        if str(database_path) == ":memory:":
            raise ValueError("persistent repository requires a filesystem database")
        if not self.database_path.is_absolute():
            raise ValueError("database path must be absolute")
        self._prepare_database_path()
        self._thread_connections = local()
        self._connection_lock = Lock()
        self._all_connections: list[sqlite3.Connection] = []
        self._closed = False
        self._connection = sqlite3.connect(self.database_path, timeout=5.0, check_same_thread=False)
        self._all_connections.append(self._connection)
        self._connection.row_factory = sqlite3.Row
        try:
            self._harden_database_permissions()
            initialize_database(self._connection)
        except BaseException:
            self._connection.close()
            raise
        self._harden_database_permissions()

    def _prepare_database_path(self) -> None:
        parent = self.database_path.parent
        self._reject_symlink_ancestors(parent)
        created_parent = False
        try:
            parent_info = os.lstat(parent)
        except FileNotFoundError:
            parent.mkdir(parents=True, mode=0o700)
            parent_info = os.lstat(parent)
            created_parent = True
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise ValueError("database parent must be a real directory")
        if parent_info.st_uid != os.getuid():
            raise PermissionError("database parent must be owned by the current user")
        if created_parent:
            self._remove_extended_acl(parent)
            os.chmod(parent, 0o700)
            self._assert_no_extended_acl(parent)
        elif stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise PermissionError("database parent must not be accessible by group or other users")
        else:
            self._assert_no_extended_acl(parent)
        try:
            database_info = os.lstat(self.database_path)
        except FileNotFoundError:
            if self._has_sidecars():
                raise RuntimeError("SQLite sidecar exists without a database")
            return
        if stat.S_ISLNK(database_info.st_mode) or not stat.S_ISREG(database_info.st_mode):
            raise ValueError("database target must be a regular file")
        if database_info.st_uid != os.getuid():
            raise PermissionError("database must be owned by the current user")
        if not self._is_nasmove_database():
            raise RuntimeError("database is not a NasMove database")
        self._secure_file(self.database_path)
        self._validate_sidecars()

    def _validate_sidecars(self) -> None:
        for sidecar in (Path(f"{self.database_path}-wal"), Path(f"{self.database_path}-shm")):
            try:
                info = os.lstat(sidecar)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError("SQLite sidecar must be a regular file")
            if info.st_uid != os.getuid():
                raise PermissionError("SQLite sidecar must be owned by the current user")
            self._secure_file(sidecar)

    def _has_sidecars(self) -> bool:
        return any(path.exists() or path.is_symlink() for path in (
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ))

    @staticmethod
    def _run_acl_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                arguments,
                env={"LC_ALL": "C"},
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise PermissionError("unable to inspect SQLite path ACL") from exc

    @classmethod
    def _has_extended_acl(cls, path: Path) -> bool:
        if sys.platform != "darwin":
            return False
        result = cls._run_acl_command(["/bin/ls", "-lde", str(path)])
        if result.returncode != 0:
            raise PermissionError(f"unable to inspect ACL for {path}")
        lines = result.stdout.splitlines()
        if not lines:
            raise PermissionError(f"unable to inspect ACL for {path}")
        return len(lines) > 1 or "+" in lines[0].split(maxsplit=1)[0]

    @classmethod
    def _assert_no_extended_acl(cls, path: Path) -> None:
        if cls._has_extended_acl(path):
            raise PermissionError(f"SQLite path has an extended ACL: {path}")

    @classmethod
    def _remove_extended_acl(cls, path: Path) -> None:
        if sys.platform != "darwin":
            return
        result = cls._run_acl_command(["/bin/chmod", "-N", str(path)])
        if result.returncode != 0:
            raise PermissionError(f"unable to remove ACL for {path}")

    @classmethod
    def _secure_file(cls, path: Path) -> None:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("SQLite path must be a regular file")
        if info.st_uid != os.getuid():
            raise PermissionError("SQLite path must be owned by the current user")
        cls._remove_extended_acl(path)
        os.chmod(path, 0o600)
        cls._assert_no_extended_acl(path)

    def _is_nasmove_database(self) -> bool:
        sources = (self.database_path, Path(f"{self.database_path}-wal"), Path(f"{self.database_path}-shm"))
        try:
            with TemporaryDirectory(dir=self.database_path.parent, prefix=".nasmove-probe-") as probe_name:
                probe_dir = Path(probe_name)
                self._remove_extended_acl(probe_dir)
                os.chmod(probe_dir, 0o700)
                self._assert_no_extended_acl(probe_dir)
                snapshots: dict[Path, os.stat_result] = {}
                for source in sources:
                    try:
                        snapshots[source] = os.lstat(source)
                    except FileNotFoundError:
                        continue
                for source, info in snapshots.items():
                    self._copy_probe_file(source, probe_dir / source.name, info)
                for source in sources:
                    try:
                        current = os.lstat(source)
                    except FileNotFoundError:
                        if source in snapshots:
                            return False
                    else:
                        if source not in snapshots or not self._same_file_snapshot(current, snapshots[source]):
                            return False
                probe_database = probe_dir / self.database_path.name
                uri = f"{probe_database.as_uri()}?mode=ro"
                connection = sqlite3.connect(uri, uri=True)
                try:
                    connection.execute("PRAGMA query_only = ON")
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        return False
                    objects = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                    ).fetchone()
                    return objects is None or has_supported_schema(connection)
                finally:
                    connection.close()
        except (OSError, sqlite3.DatabaseError, RuntimeError, ValueError):
            return False

    @staticmethod
    def _same_file_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
        return (
            first.st_dev == second.st_dev
            and first.st_ino == second.st_ino
            and first.st_mode == second.st_mode
            and first.st_size == second.st_size
            and first.st_mtime_ns == second.st_mtime_ns
        )

    @staticmethod
    def _copy_probe_file(source: Path, destination: Path, expected: os.stat_result) -> None:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            source_info = os.fstat(source_fd)
            if not stat.S_ISREG(source_info.st_mode) or source_info.st_uid != os.getuid():
                raise PermissionError("SQLite probe source must be an owned regular file")
            if not SqliteTaskRepository._same_file_snapshot(source_info, expected):
                raise RuntimeError("SQLite probe source changed during open")
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                with (
                    os.fdopen(source_fd, "rb", closefd=False) as source_stream,
                    os.fdopen(destination_fd, "wb") as destination_stream,
                ):
                    shutil.copyfileobj(source_stream, destination_stream, length=1024 * 1024)
                final_info = os.fstat(source_fd)
                if not SqliteTaskRepository._same_file_snapshot(final_info, expected):
                    raise RuntimeError("SQLite probe source changed during copy")
            finally:
                os.close(source_fd)
        except BaseException:
            try:
                os.close(source_fd)
            except OSError:
                pass
            raise

    @staticmethod
    def _reject_symlink_ancestors(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(info.st_mode):
                resolved = current.resolve()
                if current in {Path("/var"), Path("/tmp")} and resolved == Path("/private") / current.relative_to("/"):
                    continue
                raise ValueError("database path must not traverse a symlink")

    def _harden_database_permissions(self) -> None:
        self._secure_file(self.database_path)
        self._validate_sidecars()

    @property
    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise sqlite3.ProgrammingError("repository is closed")
        connection = getattr(self._thread_connections, "connection", None)
        if connection is None:
            with self._connection_lock:
                if self._closed:
                    raise sqlite3.ProgrammingError("repository is closed")
                connection = sqlite3.connect(self.database_path, timeout=5.0, check_same_thread=False)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                self._thread_connections.connection = connection
                self._all_connections.append(connection)
        return cast(sqlite3.Connection, connection)

    @_connection.setter
    def _connection(self, connection: sqlite3.Connection) -> None:
        self._thread_connections.connection = connection

    def release_thread_connection(self) -> None:
        """Called by a worker after its last operation, before its thread exits."""
        with self._connection_lock:
            connection = getattr(self._thread_connections, "connection", None)
            if connection is not None and connection in self._all_connections:
                connection.close()
                self._all_connections.remove(connection)
            self._thread_connections.connection = None

    def close(self) -> None:
        # Application shutdown waits for workers before closing their connections.
        with self._connection_lock:
            self._closed = True
            for connection in self._all_connections:
                connection.close()
            self._all_connections.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._harden_database_permissions()
        self._connection.commit()

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.rollback()

    def create_task(self, task: TaskRecord, items: Iterable[TransferItemRecord]) -> None:
        if task.state not in self._INITIAL_TASK_STATES:
            raise ValueError("task has an invalid initial state")
        self._begin()
        try:
            self._insert_connection_profile(task.connection)
            self._insert_task(task)
            batch: list[TransferItemRecord] = []
            for item in items:
                if item.task_id != task.id:
                    raise ValueError("transfer item belongs to a different task")
                if item.state not in self._INITIAL_ITEM_STATES:
                    raise ValueError("transfer item has an invalid initial state")
                batch.append(item)
                if len(batch) == self.BATCH_SIZE:
                    self._insert_item_batch(batch)
                    batch = []
            if batch:
                self._insert_item_batch(batch)
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def save_successful_connection(self, config: ConnectionConfig) -> None:
        self._begin()
        try:
            self._insert_connection_profile(config)
            self._connection.execute(
                "UPDATE connection_profiles SET last_test_ok = 1, last_test_at = ?, is_archived = 0 "
                "WHERE profile_id = ?",
                (_encode_datetime(datetime.now(UTC)), str(config.profile_id)),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def last_successful_connection(self) -> ConnectionConfig | None:
        row = self._connection.execute(
            "SELECT * FROM connection_profiles WHERE last_test_ok = 1 AND is_archived = 0 "
            "ORDER BY last_test_at DESC, profile_id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._connection_profile_from_row(row)

    def list_connection_profiles(self) -> tuple[ConnectionConfig, ...]:
        rows = self._connection.execute(
            "SELECT * FROM connection_profiles "
            "WHERE last_test_ok = 1 AND is_archived = 0 "
            "ORDER BY last_test_at DESC, display_name COLLATE NOCASE, profile_id"
        ).fetchall()
        return tuple(self._connection_profile_from_row(row) for row in rows)

    def get_connection_profile(self, profile_id: ConnectionProfileId) -> ConnectionConfig:
        row = self._connection.execute(
            "SELECT * FROM connection_profiles WHERE profile_id = ? AND is_archived = 0",
            (str(profile_id),),
        ).fetchone()
        if row is None:
            raise KeyError(profile_id)
        return self._connection_profile_from_row(row)

    def archive_connection_profile(self, profile_id: ConnectionProfileId) -> None:
        terminal_values = tuple(state.value for state in _TERMINAL_TASK_STATES)
        placeholders = ", ".join("?" for _ in terminal_values)
        self._begin()
        try:
            row = self._connection.execute(
                "SELECT is_archived FROM connection_profiles WHERE profile_id = ?",
                (str(profile_id),),
            ).fetchone()
            if row is None:
                raise KeyError(profile_id)
            if bool(row["is_archived"]):
                self._commit()
                return
            in_use = self._connection.execute(
                f"SELECT 1 FROM tasks WHERE profile_id = ? "
                f"AND state NOT IN ({placeholders}) LIMIT 1",
                (str(profile_id), *terminal_values),
            ).fetchone()
            if in_use is not None:
                raise ConnectionProfileInUse(
                    f"connection profile is required by an incomplete task: {profile_id}"
                )
            self._connection.execute(
                "UPDATE connection_profiles SET is_archived = 1 WHERE profile_id = ?",
                (str(profile_id),),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def get_credential_password(self, profile_id: ConnectionProfileId) -> str | None:
        """Return the stored password, or None when the profile has no credential."""
        row = self._connection.execute(
            "SELECT password FROM credentials WHERE profile_id = ?",
            (str(profile_id),),
        ).fetchone()
        return None if row is None else str(row[0])

    def set_credential_password(
        self, profile_id: ConnectionProfileId, password: str
    ) -> None:
        """Idempotently upsert the credential for a profile."""
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO credentials(profile_id, password) VALUES (?, ?) "
                "ON CONFLICT(profile_id) DO UPDATE SET password = excluded.password",
                (str(profile_id), password),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def delete_credential_password(self, profile_id: ConnectionProfileId) -> None:
        self._begin()
        try:
            self._connection.execute(
                "DELETE FROM credentials WHERE profile_id = ?",
                (str(profile_id),),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    @staticmethod
    def _connection_profile_from_row(row: sqlite3.Row) -> ConnectionConfig:
        return ConnectionConfig(
            profile_id=ConnectionProfileId(row["profile_id"]),
            display_name=row["display_name"],
            host=row["host"],
            port=row["port"],
            share=row["share"],
            username=row["username"],
            domain=row["domain"],
            require_encryption=bool(row["require_encryption"]),
            minimum_dialect=row["minimum_dialect"],
            max_parallel_items=row["max_parallel_items"],
        )

    def _insert_connection_profile(self, config: ConnectionConfig) -> None:
        self._connection.execute(
            """INSERT INTO connection_profiles(
                profile_id, display_name, host, port, share, username, domain,
                require_encryption, minimum_dialect, max_parallel_items, keychain_account
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                display_name = excluded.display_name, host = excluded.host,
                port = excluded.port, share = excluded.share, username = excluded.username,
                domain = excluded.domain, require_encryption = excluded.require_encryption,
                minimum_dialect = excluded.minimum_dialect,
                max_parallel_items = excluded.max_parallel_items, is_archived = 0""",
            (
                str(config.profile_id),
                config.display_name,
                config.host,
                config.port,
                config.share,
                config.username,
                config.domain,
                _bool_value(config.require_encryption),
                config.minimum_dialect,
                config.max_parallel_items,
                str(config.profile_id),
            ),
        )

    def _insert_task(self, task: TaskRecord) -> None:
        config = task.connection
        self._connection.execute(
            """INSERT INTO tasks(
                task_id, name, action, profile_id, connection_display_name, connection_host,
                connection_port, connection_share, connection_username, connection_domain,
                connection_require_encryption, connection_minimum_dialect,
                connection_max_parallel_items, target_root,
                conflict_policy, conflict_strategy, verification_policy, state, queue_position, recovery_generation,
                total_files, total_bytes, copied_bytes, verified_bytes, revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(task.id),
                task.name,
                task.action.value,
                str(config.profile_id),
                config.display_name,
                config.host,
                config.port,
                config.share,
                config.username,
                config.domain,
                _bool_value(config.require_encryption),
                config.minimum_dialect,
                config.max_parallel_items,
                task.target_root.value,
                "auto_rename",
                task.conflict_policy.value,
                task.verification_policy.value,
                task.state.value,
                task.queue_position,
                task.recovery_generation,
                task.total_files,
                task.total_bytes,
                task.copied_bytes,
                task.verified_bytes,
                task.revision,
                _encode_datetime(task.created_at),
                _encode_datetime(task.updated_at),
            ),
        )

    def _insert_item_batch(self, items: Sequence[TransferItemRecord]) -> None:
        self._connection.executemany(
            """INSERT INTO transfer_items(
                item_id, task_id, source_path, relative_path, final_path, temp_path,
                source_device, source_inode, source_kind, source_size, source_mtime_ns,
                state, confirmed_offset, retry_count, sha256, full_hash_verified,
                target_file_id, final_size, committed_at, verified_session_generation, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    str(item.id),
                    str(item.task_id),
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
                    item.confirmed_offset,
                    item.retry_count,
                    item.sha256,
                    _bool_value(item.full_hash_verified),
                    item.target_file_id,
                    item.final_size,
                    None if item.committed_at is None else _encode_datetime(item.committed_at),
                    item.verified_session_generation,
                    item.revision,
                )
                for item in items
            ],
        )

    def get_task(self, task_id: TaskId) -> TaskRecord:
        row = self._connection.execute("SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)).fetchone()
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return self._task_from_row(row)

    def get_item(self, item_id: TransferItemId) -> TransferItemRecord:
        row = self._connection.execute(
            "SELECT * FROM transfer_items WHERE item_id = ?", (str(item_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"item not found: {item_id}")
        return self._item_from_row(row)

    def list_items(self, task_id: TaskId) -> list[TransferItemRecord]:
        rows = self._connection.execute(
            "SELECT * FROM transfer_items WHERE task_id = ? ORDER BY item_id",
            (str(task_id),),
        )
        return [self._item_from_row(row) for row in rows]

    def next_queued_task(self) -> TaskRecord | None:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE state = ? ORDER BY queue_position, created_at, task_id LIMIT 1",
            (TaskState.QUEUED.value,),
        ).fetchone()
        return None if row is None else self._task_from_row(row)

    def transition_task(self, task_id: TaskId, expected: TaskState, target: TaskState) -> None:
        assert_task_transition(expected, target)
        now = _encode_datetime(datetime.now(UTC))
        self._begin()
        try:
            result = self._connection.execute(
                "UPDATE tasks SET state = ?, revision = revision + 1, updated_at = ? "
                "WHERE task_id = ? AND state = ?",
                (target.value, now, str(task_id), expected.value),
            )
            if result.rowcount != 1:
                raise ConcurrentStateChange(f"task state changed concurrently: {task_id}")
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def transition_item(self, item_id: TransferItemId, expected: ItemState, target: ItemState) -> None:
        assert_item_transition(expected, target)
        self._begin()
        try:
            result = self._connection.execute(
                "UPDATE transfer_items SET state = ?, revision = revision + 1 "
                "WHERE item_id = ? AND state = ?",
                (target.value, str(item_id), expected.value),
            )
            if result.rowcount != 1:
                raise ConcurrentStateChange(f"item state changed concurrently: {item_id}")
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def update_item_metadata(self, item: TransferItemRecord, expected_revision: int) -> None:
        current = self.get_item(item.id)
        if current.state != item.state:
            raise ConcurrentStateChange(f"item state changed concurrently: {item.id}")
        self._begin()
        try:
            result = self._connection.execute(
                """UPDATE transfer_items SET final_path = ?, confirmed_offset = ?, retry_count = ?,
                    sha256 = ?, full_hash_verified = ?, target_file_id = ?, final_size = ?,
                    committed_at = ?, verified_session_generation = ?, revision = revision + 1
                    WHERE item_id = ? AND state = ? AND revision = ?""",
                (
                    item.final_path.value,
                    item.confirmed_offset,
                    item.retry_count,
                    item.sha256,
                    _bool_value(item.full_hash_verified),
                    item.target_file_id,
                    item.final_size,
                    None if item.committed_at is None else _encode_datetime(item.committed_at),
                    item.verified_session_generation,
                    str(item.id),
                    item.state.value,
                    expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise ConcurrentStateChange(f"item revision changed concurrently: {item.id}")
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._begin()
        try:
            row = self._connection.execute(
                "SELECT source_size, confirmed_offset FROM transfer_items WHERE item_id = ?",
                (str(checkpoint.item_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"item not found: {checkpoint.item_id}")
            if checkpoint.remote_size > row[0]:
                raise ValueError("checkpoint remote size exceeds source size")
            if checkpoint.confirmed_offset < row[1]:
                raise ValueError("checkpoint confirmed offset must not go backwards")
            self._insert_checkpoint(checkpoint)
            self._update_item_checkpoint_offset(checkpoint)
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def _insert_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._connection.execute(
            """INSERT INTO checkpoints(
                item_id, confirmed_offset, remote_size, window_start, window_length,
                window_sha256, session_generation, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(checkpoint.item_id),
                checkpoint.confirmed_offset,
                checkpoint.remote_size,
                checkpoint.window_start,
                checkpoint.window_length,
                checkpoint.window_sha256,
                checkpoint.session_generation,
                _encode_datetime(checkpoint.created_at),
            ),
        )

    def _update_item_checkpoint_offset(self, checkpoint: Checkpoint) -> None:
        result = self._connection.execute(
            "UPDATE transfer_items SET confirmed_offset = ?, revision = revision + 1 "
            "WHERE item_id = ? AND confirmed_offset <= ?",
            (checkpoint.confirmed_offset, str(checkpoint.item_id), checkpoint.confirmed_offset),
        )
        if result.rowcount != 1:
            raise ConcurrentStateChange(f"item checkpoint changed concurrently: {checkpoint.item_id}")

    def checkpoints_desc(self, item_id: TransferItemId) -> list[Checkpoint]:
        rows = self._connection.execute(
            "SELECT * FROM checkpoints WHERE item_id = ? ORDER BY created_at DESC, checkpoint_id DESC",
            (str(item_id),),
        )
        return [
            Checkpoint(
                item_id=TransferItemId(row["item_id"]),
                confirmed_offset=row["confirmed_offset"],
                remote_size=row["remote_size"],
                window_start=row["window_start"],
                window_length=row["window_length"],
                window_sha256=row["window_sha256"],
                session_generation=row["session_generation"],
                created_at=_decode_datetime(row["created_at"]),
            )
            for row in rows
        ]

    def list_incomplete_tasks(self) -> list[TaskRecord]:
        placeholders = ",".join("?" for _ in _TERMINAL_TASK_STATES)
        rows = self._connection.execute(
            f"SELECT * FROM tasks WHERE state NOT IN ({placeholders}) ORDER BY queue_position, created_at, task_id",
            tuple(state.value for state in _TERMINAL_TASK_STATES),
        )
        return [self._task_from_row(row) for row in rows]

    def list_tasks(self) -> list[TaskRecord]:
        return [self._task_from_row(row) for row in self._connection.execute(
            "SELECT * FROM tasks ORDER BY queue_position, created_at, task_id"
        )]

    def list_item_page(self, task_id: TaskId, offset: int = 0) -> list[TransferItemRecord]:
        return [self._item_from_row(row) for row in self._connection.execute(
            "SELECT * FROM transfer_items WHERE task_id = ? ORDER BY item_id LIMIT 100 OFFSET ?",
            (task_id, max(0, offset)),
        )]

    def record_ui_error(self, task_id: TaskId | None, code: str) -> None:
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO events(task_id,event_type,error_code,occurred_at) VALUES(?,?,?,?)",
                (task_id, "execution_error", code, _encode_datetime(datetime.now(UTC))),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def last_ui_error(self, task_id: TaskId | None) -> str | None:
        row = self._connection.execute(
            "SELECT error_code FROM events WHERE task_id IS ? AND event_type = ? "
            "ORDER BY event_id DESC LIMIT 1", (task_id, "execution_error"),
        ).fetchone()
        return None if row is None else str(row[0])

    def record_deletion_outcome(
        self, task_id: TaskId | None, item_id: TransferItemId, code: str, summary: str
    ) -> None:
        """Persist why a source deletion ended the way it did, for diagnosis.

        Stored in the existing ``events`` table under its own ``event_type`` so
        no schema change is required.
        """
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO events(task_id,item_id,event_type,error_code,summary,occurred_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    task_id,
                    item_id,
                    "deletion_outcome",
                    code,
                    summary,
                    _encode_datetime(datetime.now(UTC)),
                ),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def last_deletion_outcome(self, item_id: TransferItemId) -> tuple[str, str | None] | None:
        row = self._connection.execute(
            "SELECT error_code, summary FROM events WHERE item_id IS ? AND event_type = ? "
            "ORDER BY event_id DESC LIMIT 1", (item_id, "deletion_outcome"),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), None if row[1] is None else str(row[1])

    def last_deletion_outcome_for_task(
        self, task_id: TaskId | None
    ) -> tuple[str, str | None] | None:
        """Read the task's most recent deletion outcome, for the task summary."""
        row = self._connection.execute(
            "SELECT error_code, summary FROM events WHERE task_id IS ? AND event_type = ? "
            "ORDER BY event_id DESC LIMIT 1", (task_id, "deletion_outcome"),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), None if row[1] is None else str(row[1])

    def mark_active_tasks_interrupted(self) -> int:
        self._begin()
        try:
            placeholders = ",".join("?" for _ in _ACTIVE_TASK_STATES)
            unfinished_placeholders = ",".join("?" for _ in _TERMINAL_TASK_STATES)
            active_count = int(self._connection.execute(
                f"SELECT count(*) FROM tasks WHERE state IN ({placeholders})",
                tuple(state.value for state in _ACTIVE_TASK_STATES),
            ).fetchone()[0])
            result = self._connection.execute(
                f"UPDATE tasks SET state = CASE WHEN state IN ({placeholders}) "
                f"THEN ? ELSE state END, recovery_generation = recovery_generation + 1, "
                f"revision = revision + 1, updated_at = ? WHERE state NOT IN ({unfinished_placeholders})",
                tuple(state.value for state in _ACTIVE_TASK_STATES)
                + (TaskState.INTERRUPTED.value, _encode_datetime(datetime.now(UTC)))
                + tuple(state.value for state in _TERMINAL_TASK_STATES),
            )
            if result.rowcount < active_count:
                raise ConcurrentStateChange("active task recovery update was incomplete")
            self._connection.execute(
                f"UPDATE transfer_items SET full_hash_verified = 0, "
                f"verified_session_generation = NULL, revision = revision + 1 "
                f"WHERE task_id IN (SELECT task_id FROM tasks WHERE state NOT IN ({unfinished_placeholders})) "
                "AND (full_hash_verified <> 0 OR verified_session_generation IS NOT NULL)",
                tuple(state.value for state in _TERMINAL_TASK_STATES),
            )
            count = active_count
            self._commit()
            return count
        except BaseException:
            self._rollback()
            raise

    def reorder_queued_tasks(self, task_ids: Sequence[TaskId]) -> None:
        requested = [str(task_id) for task_id in task_ids]
        if len(requested) != len(set(requested)):
            raise ValueError("queued task IDs must not contain duplicates")
        self._begin()
        try:
            rows = self._connection.execute(
                "SELECT task_id FROM tasks WHERE state = ? ORDER BY queue_position, created_at, task_id",
                (TaskState.QUEUED.value,),
            ).fetchall()
            current = [row[0] for row in rows]
            if set(requested) != set(current) or len(requested) != len(current):
                raise ValueError("queued task IDs must be a complete permutation")
            now = _encode_datetime(datetime.now(UTC))
            for position, task_id in enumerate(requested):
                self._connection.execute(
                    "UPDATE tasks SET queue_position = ?, revision = revision + 1, updated_at = ? "
                    "WHERE task_id = ? AND state = ?",
                    (position, now, task_id, TaskState.QUEUED.value),
                )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        connection = ConnectionConfig(
            profile_id=ConnectionProfileId(row["profile_id"]),
            display_name=row["connection_display_name"],
            host=row["connection_host"],
            port=row["connection_port"],
            share=row["connection_share"],
            username=row["connection_username"],
            domain=row["connection_domain"],
            require_encryption=bool(row["connection_require_encryption"]),
            minimum_dialect=row["connection_minimum_dialect"],
            max_parallel_items=row["connection_max_parallel_items"],
        )
        return TaskRecord(
            id=TaskId(row["task_id"]),
            name=row["name"],
            action=TransferAction(row["action"]),
            connection=connection,
            target_root=RemotePath(row["target_root"]),
            conflict_policy=ConflictPolicy(row["conflict_strategy"]),
            verification_policy=VerificationPolicy(row["verification_policy"]),
            state=TaskState(row["state"]),
            queue_position=row["queue_position"],
            recovery_generation=row["recovery_generation"],
            total_files=row["total_files"],
            total_bytes=row["total_bytes"],
            copied_bytes=row["copied_bytes"],
            verified_bytes=row["verified_bytes"],
            revision=row["revision"],
            created_at=_decode_datetime(row["created_at"]),
            updated_at=_decode_datetime(row["updated_at"]),
        )

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> TransferItemRecord:
        committed_at = row["committed_at"]
        return TransferItemRecord(
            id=TransferItemId(row["item_id"]),
            task_id=TaskId(row["task_id"]),
            source_path=Path(row["source_path"]),
            relative_path=PurePosixPath(row["relative_path"]),
            final_path=RemotePath(row["final_path"]),
            temp_path=RemotePath(row["temp_path"]),
            source_fingerprint=SourceFingerprint(
                device=row["source_device"],
                inode=row["source_inode"],
                kind=SourceKind(row["source_kind"]),
                size=row["source_size"],
                mtime_ns=row["source_mtime_ns"],
            ),
            state=ItemState(row["state"]),
            confirmed_offset=row["confirmed_offset"],
            retry_count=row["retry_count"],
            sha256=row["sha256"],
            full_hash_verified=bool(row["full_hash_verified"]),
            target_file_id=row["target_file_id"],
            final_size=row["final_size"],
            committed_at=None if committed_at is None else _decode_datetime(committed_at),
            verified_session_generation=row["verified_session_generation"],
            revision=row["revision"],
        )


__all__ = ["SqliteTaskRepository"]
