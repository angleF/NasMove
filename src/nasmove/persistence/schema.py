from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connection_profiles (
    profile_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
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
    action TEXT NOT NULL,
    profile_id TEXT NOT NULL REFERENCES connection_profiles(profile_id),
    connection_display_name TEXT NOT NULL,
    connection_host TEXT NOT NULL,
    connection_port INTEGER NOT NULL,
    connection_share TEXT NOT NULL,
    connection_username TEXT NOT NULL,
    connection_domain TEXT,
    connection_require_encryption INTEGER NOT NULL CHECK (connection_require_encryption IN (0, 1)),
    connection_minimum_dialect TEXT NOT NULL,
    target_root TEXT NOT NULL,
    conflict_policy TEXT NOT NULL,
    verification_policy TEXT NOT NULL,
    state TEXT NOT NULL,
    queue_position INTEGER NOT NULL,
    recovery_generation INTEGER NOT NULL,
    total_files INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    copied_bytes INTEGER NOT NULL,
    verified_bytes INTEGER NOT NULL,
    revision INTEGER NOT NULL,
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
    source_device INTEGER NOT NULL,
    source_inode INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_size INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    state TEXT NOT NULL,
    confirmed_offset INTEGER NOT NULL,
    retry_count INTEGER NOT NULL,
    sha256 TEXT,
    full_hash_verified INTEGER NOT NULL CHECK (full_hash_verified IN (0, 1)),
    target_file_id TEXT,
    final_size INTEGER,
    committed_at TEXT,
    verified_session_generation INTEGER,
    revision INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_task ON transfer_items(task_id, item_id);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES transfer_items(item_id) ON DELETE CASCADE,
    confirmed_offset INTEGER NOT NULL,
    remote_size INTEGER NOT NULL,
    window_start INTEGER NOT NULL,
    window_length INTEGER NOT NULL,
    window_sha256 TEXT NOT NULL,
    session_generation INTEGER NOT NULL,
    created_at TEXT NOT NULL
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


def initialize_database(connection: sqlite3.Connection) -> None:
    """Configure SQLite durability and create the current schema."""
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    connection.commit()
