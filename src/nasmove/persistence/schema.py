from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import TypeVar

from nasmove.core.states import ItemState, TaskState
from nasmove.core.transitions import ITEM_TRANSITIONS, TASK_TRANSITIONS

SCHEMA_VERSION = "2"
State = TypeVar("State", TaskState, ItemState)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connection_profiles (
    profile_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    share TEXT NOT NULL,
    username TEXT NOT NULL,
    domain TEXT,
    require_encryption INTEGER NOT NULL CHECK (require_encryption IN (0, 1)),
    minimum_dialect TEXT NOT NULL,
    keychain_account TEXT NOT NULL,
    last_test_ok INTEGER CHECK (last_test_ok IN (0, 1)),
    last_test_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('copy', 'move')),
    profile_id TEXT NOT NULL REFERENCES connection_profiles(profile_id),
    connection_display_name TEXT NOT NULL,
    connection_host TEXT NOT NULL,
    connection_port INTEGER NOT NULL CHECK (connection_port BETWEEN 1 AND 65535),
    connection_share TEXT NOT NULL,
    connection_username TEXT NOT NULL,
    connection_domain TEXT,
    connection_require_encryption INTEGER NOT NULL CHECK (connection_require_encryption IN (0, 1)),
    connection_minimum_dialect TEXT NOT NULL,
    target_root TEXT NOT NULL,
    conflict_policy TEXT NOT NULL CHECK (conflict_policy IN ('auto_rename')),
    verification_policy TEXT NOT NULL CHECK (verification_policy IN ('full')),
    state TEXT NOT NULL CHECK (state IN ('draft', 'preflight', 'queued', 'running',
        'interrupted', 'waiting_for_network', 'paused', 'verifying', 'committing',
        'deleting_source', 'completed', 'completed_with_warnings', 'failed', 'canceled')),
    queue_position INTEGER NOT NULL CHECK (queue_position >= 0),
    recovery_generation INTEGER NOT NULL CHECK (recovery_generation >= 0),
    total_files INTEGER NOT NULL CHECK (total_files >= 0),
    total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0),
    copied_bytes INTEGER NOT NULL CHECK (copied_bytes >= 0 AND copied_bytes <= total_bytes),
    verified_bytes INTEGER NOT NULL CHECK (verified_bytes >= 0 AND verified_bytes <= total_bytes),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(state, queue_position, created_at, task_id);
CREATE TABLE IF NOT EXISTS transfer_items (
    item_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    final_path TEXT NOT NULL,
    temp_path TEXT NOT NULL,
    source_device INTEGER NOT NULL CHECK (source_device >= 0),
    source_inode INTEGER NOT NULL CHECK (source_inode >= 0),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('file', 'empty_directory')),
    source_size INTEGER NOT NULL CHECK (source_size >= 0),
    source_mtime_ns INTEGER NOT NULL CHECK (source_mtime_ns >= 0),
    state TEXT NOT NULL CHECK (state IN ('planned', 'transferring', 'transferred', 'verifying',
        'verified', 'committed', 'source_delete_authorized', 'done', 'interrupted',
        'waiting_retry', 'source_changed', 'verify_failed', 'source_retained', 'skipped')),
    confirmed_offset INTEGER NOT NULL CHECK (confirmed_offset >= 0 AND confirmed_offset <= source_size),
    retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
    sha256 TEXT,
    full_hash_verified INTEGER NOT NULL CHECK (full_hash_verified IN (0, 1)),
    target_file_id TEXT,
    final_size INTEGER CHECK (final_size IS NULL OR final_size >= 0),
    committed_at TEXT,
    verified_session_generation INTEGER,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    CHECK (full_hash_verified = 0 OR (sha256 IS NOT NULL AND verified_session_generation IS NOT NULL)),
    CHECK (verified_session_generation IS NULL OR verified_session_generation > 0)
);
CREATE INDEX IF NOT EXISTS idx_items_task ON transfer_items(task_id, item_id);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES transfer_items(item_id) ON DELETE CASCADE,
    confirmed_offset INTEGER NOT NULL CHECK (confirmed_offset >= 0),
    remote_size INTEGER NOT NULL CHECK (remote_size >= 0),
    window_start INTEGER NOT NULL CHECK (window_start >= 0),
    window_length INTEGER NOT NULL CHECK (window_length >= 0),
    window_sha256 TEXT NOT NULL,
    session_generation INTEGER NOT NULL CHECK (session_generation > 0),
    created_at TEXT NOT NULL,
    CHECK (window_start + window_length <= confirmed_offset),
    CHECK (confirmed_offset <= remote_size)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_item_desc
    ON checkpoints(item_id, created_at DESC, checkpoint_id DESC);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
    item_id TEXT REFERENCES transfer_items(item_id) ON DELETE CASCADE,
    operation TEXT NOT NULL,
    error_category TEXT,
    error_code TEXT,
    summary TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
    item_id TEXT REFERENCES transfer_items(item_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    error_category TEXT,
    error_code TEXT,
    summary TEXT,
    occurred_at TEXT NOT NULL
);
"""


def _transition_trigger[State: (TaskState, ItemState)](
    table: str, mapping: Mapping[State, frozenset[State]], trigger_name: str
) -> str:
    clauses = []
    for current in sorted(mapping, key=lambda state: state.value):
        targets = sorted(mapping[current], key=lambda state: state.value)
        if targets:
            values = ", ".join(f"'{state.value}'" for state in targets)
            clauses.append(f"(OLD.state = '{current.value}' AND NEW.state IN ({values}))")
    allowed = " OR ".join(clauses) or "0"
    return (
        f"DROP TRIGGER IF EXISTS {trigger_name};"
        f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OF state ON {table} "
        f"WHEN OLD.state <> NEW.state AND NOT ({allowed}) "
        "BEGIN SELECT RAISE(ABORT, 'invalid state transition'); END;"
    )


def _trigger_sql() -> str:
    return (
        _transition_trigger("tasks", TASK_TRANSITIONS, "tasks_state_guard")
        + _transition_trigger("transfer_items", ITEM_TRANSITIONS, "transfer_items_state_guard")
        + "DROP TRIGGER IF EXISTS tasks_initial_state_guard;"
        + "CREATE TRIGGER tasks_initial_state_guard BEFORE INSERT ON tasks "
        + "WHEN NEW.state NOT IN ('draft', 'preflight', 'queued') "
        + "BEGIN SELECT RAISE(ABORT, 'invalid initial task state'); END;"
        + "DROP TRIGGER IF EXISTS transfer_items_initial_state_guard;"
        + "CREATE TRIGGER transfer_items_initial_state_guard BEFORE INSERT ON transfer_items "
        + "WHEN NEW.state NOT IN ('planned', 'skipped') "
        + "BEGIN SELECT RAISE(ABORT, 'invalid initial item state'); END;"
    )


def initialize_database(connection: sqlite3.Connection) -> None:
    """Configure SQLite durability and create the current schema."""
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
    ).fetchone()
    if existing is None:
        user_objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if user_objects:
            raise RuntimeError("database contains user objects but no schema version")
    else:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        if version is None or version[0] != SCHEMA_VERSION:
            found = "missing" if version is None else str(version[0])
            raise RuntimeError(f"unsupported schema version: {found}")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(SCHEMA_SQL + _trigger_sql())
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    connection.commit()
