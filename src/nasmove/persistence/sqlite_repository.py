from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Self, cast

from nasmove.core.errors import ConcurrentStateChange
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
from nasmove.persistence.schema import initialize_database

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

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        if str(database_path) != ":memory:":
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite_target: Path | str = ":memory:" if str(database_path) == ":memory:" else self.database_path
        self._connection = sqlite3.connect(sqlite_target, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        initialize_database(self._connection)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = cast(sqlite3.Connection, None)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._connection.commit()

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.rollback()

    def create_task(self, task: TaskRecord, items: Iterable[TransferItemRecord]) -> None:
        self._begin()
        try:
            self._insert_connection_profile(task.connection)
            self._insert_task(task)
            batch: list[TransferItemRecord] = []
            for item in items:
                if item.task_id != task.id:
                    raise ValueError("transfer item belongs to a different task")
                batch.append(item)
                if len(batch) == self.BATCH_SIZE:
                    self._insert_item_batch(batch)
                    batch.clear()
            if batch:
                self._insert_item_batch(batch)
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def _insert_connection_profile(self, config: ConnectionConfig) -> None:
        self._connection.execute(
            """INSERT INTO connection_profiles(
                profile_id, display_name, host, port, share, username, domain,
                require_encryption, minimum_dialect, keychain_account
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                display_name = excluded.display_name, host = excluded.host,
                port = excluded.port, share = excluded.share, username = excluded.username,
                domain = excluded.domain, require_encryption = excluded.require_encryption,
                minimum_dialect = excluded.minimum_dialect""",
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
                str(config.profile_id),
            ),
        )

    def _insert_task(self, task: TaskRecord) -> None:
        config = task.connection
        self._connection.execute(
            """INSERT INTO tasks(
                task_id, name, action, profile_id, connection_display_name, connection_host,
                connection_port, connection_share, connection_username, connection_domain,
                connection_require_encryption, connection_minimum_dialect, target_root,
                conflict_policy, verification_policy, state, queue_position, recovery_generation,
                total_files, total_bytes, copied_bytes, verified_bytes, revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                task.target_root.value,
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

    def mark_active_tasks_interrupted(self) -> int:
        self._begin()
        try:
            placeholders = ",".join("?" for _ in _ACTIVE_TASK_STATES)
            result = self._connection.execute(
                f"UPDATE tasks SET state = ?, revision = revision + 1, updated_at = ? "
                f"WHERE state IN ({placeholders})",
                (TaskState.INTERRUPTED.value, _encode_datetime(datetime.now(UTC)))
                + tuple(state.value for state in _ACTIVE_TASK_STATES),
            )
            count = result.rowcount
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
        )
        return TaskRecord(
            id=TaskId(row["task_id"]),
            name=row["name"],
            action=TransferAction(row["action"]),
            connection=connection,
            target_root=RemotePath(row["target_root"]),
            conflict_policy=ConflictPolicy(row["conflict_policy"]),
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
