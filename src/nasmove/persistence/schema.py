from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import TypeVar

from nasmove.core.states import ItemState, TaskState
from nasmove.core.transitions import ITEM_TRANSITIONS, TASK_TRANSITIONS

SCHEMA_VERSION = "7"
LEGACY_SCHEMA_VERSIONS = ("6", "5", "4", "3")
SCHEMA_HASH_KEY = "schema_hash"
CORE_TABLES = frozenset(
    {
        "schema_meta",
        "connection_profiles",
        "tasks",
        "transfer_items",
        "checkpoints",
        "attempts",
        "events",
    }
)
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

PROFILE_ARCHIVE_SQL = """
ALTER TABLE connection_profiles
ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1));
"""

CONFLICT_STRATEGY_SQL = """
ALTER TABLE tasks
ADD COLUMN conflict_strategy TEXT NOT NULL DEFAULT 'keep_both'
CHECK (conflict_strategy IN ('keep_both', 'overwrite', 'skip', 'overwrite_if_newer', 'ask'));
"""

PARALLEL_ITEMS_SQL = """
ALTER TABLE connection_profiles ADD COLUMN max_parallel_items INTEGER NOT NULL DEFAULT 2
CHECK (max_parallel_items BETWEEN 1 AND 4);
ALTER TABLE tasks ADD COLUMN connection_max_parallel_items INTEGER NOT NULL DEFAULT 2
CHECK (connection_max_parallel_items BETWEEN 1 AND 4);
"""

# The v7 addition. Kept out of SCHEMA_SQL so the frozen legacy fingerprints for
# v3..v6 continue to describe exactly the schema text those databases carry.
CREDENTIALS_SQL = """
CREATE TABLE IF NOT EXISTS credentials (
    profile_id TEXT PRIMARY KEY REFERENCES connection_profiles(profile_id),
    password TEXT NOT NULL
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
        f"CREATE TRIGGER IF NOT EXISTS {trigger_name} BEFORE UPDATE OF state ON {table} "
        f"WHEN OLD.state <> NEW.state AND NOT ({allowed}) "
        "BEGIN SELECT RAISE(ABORT, 'invalid state transition'); END;"
    )


def _trigger_sql() -> str:
    return (
        _transition_trigger("tasks", TASK_TRANSITIONS, "tasks_state_guard")
        + _transition_trigger("transfer_items", ITEM_TRANSITIONS, "transfer_items_state_guard")
        + "CREATE TRIGGER IF NOT EXISTS tasks_initial_state_guard BEFORE INSERT ON tasks "
        + "WHEN NEW.state NOT IN ('draft', 'preflight', 'queued') "
        + "BEGIN SELECT RAISE(ABORT, 'invalid initial task state'); END;"
        + "CREATE TRIGGER IF NOT EXISTS transfer_items_initial_state_guard BEFORE INSERT ON transfer_items "
        + "WHEN NEW.state NOT IN ('planned', 'skipped') "
        + "BEGIN SELECT RAISE(ABORT, 'invalid initial item state'); END;"
    )


def _legacy_trigger_sql() -> str:
    legacy_item_transitions = dict(ITEM_TRANSITIONS)
    verified_targets = set(ITEM_TRANSITIONS[ItemState.VERIFIED])
    verified_targets.discard(ItemState.SKIPPED)
    legacy_item_transitions[ItemState.VERIFIED] = frozenset(verified_targets)
    return (
        _transition_trigger("tasks", TASK_TRANSITIONS, "tasks_state_guard")
        + _transition_trigger(
            "transfer_items",
            legacy_item_transitions,
            "transfer_items_state_guard",
        )
        + "CREATE TRIGGER IF NOT EXISTS tasks_initial_state_guard BEFORE INSERT ON tasks "
        + "WHEN NEW.state NOT IN ('draft', 'preflight', 'queued') "
        + "BEGIN SELECT RAISE(ABORT, 'invalid initial task state'); END;"
        + "CREATE TRIGGER IF NOT EXISTS transfer_items_initial_state_guard BEFORE INSERT ON transfer_items "
        + "WHEN NEW.state NOT IN ('planned', 'skipped') "
        + "BEGIN SELECT RAISE(ABORT, 'invalid initial item state'); END;"
    )


def schema_fingerprint(connection: sqlite3.Connection) -> str:
    """Return a stable digest of the user schema, excluding SQLite internals."""
    objects = sorted(
        (
            row[0],
            row[1],
            row[2] or "",
            row[3] or "",
        )
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%'"
        )
    )
    payload = json.dumps(objects, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expected_schema_fingerprint() -> str:
    """Build the canonical schema in memory and fingerprint its SQLite objects."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            SCHEMA_SQL
            + _trigger_sql()
            + PROFILE_ARCHIVE_SQL
            + CONFLICT_STRATEGY_SQL
            + PARALLEL_ITEMS_SQL
            + CREDENTIALS_SQL
        )
        return schema_fingerprint(connection)
    finally:
        connection.close()


def _expected_legacy_schema_fingerprint(version: str) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        if version == "6":
            connection.executescript(
                SCHEMA_SQL
                + _trigger_sql()
                + PROFILE_ARCHIVE_SQL
                + CONFLICT_STRATEGY_SQL
                + PARALLEL_ITEMS_SQL
            )
        elif version == "5":
            connection.executescript(
                SCHEMA_SQL
                + _trigger_sql()
                + PROFILE_ARCHIVE_SQL
                + CONFLICT_STRATEGY_SQL
            )
        else:
            suffix = PROFILE_ARCHIVE_SQL if version == "4" else ""
            connection.executescript(SCHEMA_SQL + _legacy_trigger_sql() + suffix)
        return schema_fingerprint(connection)
    finally:
        connection.close()


def _has_schema_identity(
    connection: sqlite3.Connection, *, version: str, expected_hash: str
) -> bool:
    try:
        stored_version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        stored_hash = connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (SCHEMA_HASH_KEY,)
        ).fetchone()
    except sqlite3.DatabaseError:
        return False
    if stored_version is None or stored_version[0] != version or stored_hash is None:
        return False
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not CORE_TABLES <= tables:
        return False
    actual_hash = schema_fingerprint(connection)
    return bool(actual_hash == stored_hash[0] == expected_hash)


def has_current_schema(connection: sqlite3.Connection) -> bool:
    """Check version, stored digest, required tables, and actual schema structure."""
    return _has_schema_identity(
        connection,
        version=SCHEMA_VERSION,
        expected_hash=expected_schema_fingerprint(),
    )


def _legacy_schema_version(connection: sqlite3.Connection) -> str | None:
    for version in LEGACY_SCHEMA_VERSIONS:
        if _has_schema_identity(
            connection,
            version=version,
            expected_hash=_expected_legacy_schema_fingerprint(version),
        ):
            return version
    return None


def has_supported_schema(connection: sqlite3.Connection) -> bool:
    """Accept current schema and the one exact legacy identity that can be migrated."""
    return has_current_schema(connection) or _legacy_schema_version(connection) is not None


def _write_schema_identity(connection: sqlite3.Connection, schema_hash: str) -> None:
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_HASH_KEY, schema_hash),
    )


def _migrate_legacy_schema(connection: sqlite3.Connection, version: str) -> None:
    expected_hash = expected_schema_fingerprint()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if version == "3":
            connection.execute(PROFILE_ARCHIVE_SQL)
        if version in ("3", "4"):
            connection.execute(CONFLICT_STRATEGY_SQL)
            connection.execute("DROP TRIGGER transfer_items_state_guard")
            connection.execute(
                _transition_trigger(
                    "transfer_items",
                    ITEM_TRANSITIONS,
                    "transfer_items_state_guard",
                )
            )
        if version in ("3", "4", "5"):
            for statement in PARALLEL_ITEMS_SQL.split(";"):
                if statement.strip():
                    connection.execute(statement)
        for statement in CREDENTIALS_SQL.split(";"):
            if statement.strip():
                connection.execute(statement)
        actual_hash = schema_fingerprint(connection)
        if actual_hash != expected_hash:
            raise RuntimeError("migrated database does not match the current schema")
        _write_schema_identity(connection, actual_hash)
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def initialize_database(connection: sqlite3.Connection) -> None:
    """Configure SQLite durability and create the current schema."""
    legacy_version: str | None = None
    user_objects = connection.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if not user_objects:
        is_new_database = True
        is_legacy_database = False
    else:
        is_new_database = False
        legacy_version = _legacy_schema_version(connection)
        is_legacy_database = legacy_version is not None
        if not has_current_schema(connection) and not is_legacy_database:
            has_meta_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
            ).fetchone()
            version = (
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'version'"
                ).fetchone()
                if has_meta_table is not None
                else None
            )
            found = "missing" if version is None else str(version[0])
            raise RuntimeError(f"unsupported schema identity: {found}")

    # Re-read identity immediately before any persistent PRAGMA can run.
    if is_new_database:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone() is not None:
            raise RuntimeError("database schema changed during initialization")
    elif is_legacy_database:
        if _legacy_schema_version(connection) != legacy_version:
            raise RuntimeError("database schema changed during initialization")
    elif not has_current_schema(connection):
        raise RuntimeError("database schema changed during initialization")

    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if is_legacy_database:
        assert legacy_version is not None
        _migrate_legacy_schema(connection, legacy_version)
        return
    if not is_new_database:
        connection.commit()
        return

    expected_hash = expected_schema_fingerprint()
    try:
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + SCHEMA_SQL
            + _trigger_sql()
            + PROFILE_ARCHIVE_SQL
            + CONFLICT_STRATEGY_SQL
            + PARALLEL_ITEMS_SQL
            + CREDENTIALS_SQL
        )
        actual_hash = schema_fingerprint(connection)
        if actual_hash != expected_hash:
            raise RuntimeError("database schema does not match the current schema")
        _write_schema_identity(connection, actual_hash)
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
