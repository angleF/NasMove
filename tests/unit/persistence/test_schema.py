import sqlite3
import subprocess
import sys

from nasmove.persistence.schema import (
    SCHEMA_HASH_KEY,
    SCHEMA_VERSION,
    initialize_database,
    schema_fingerprint,
)
from nasmove.persistence.sqlite_repository import SqliteTaskRepository


def test_schema_initializes_durable_pragmas_and_required_tables(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "nasmove.db")
    initialize_database(connection)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "schema_meta",
        "connection_profiles",
        "tasks",
        "transfer_items",
        "checkpoints",
        "attempts",
        "events",
    } <= names
    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()[0] == SCHEMA_VERSION == "3"
    schema_hash = connection.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (SCHEMA_HASH_KEY,)
    ).fetchone()[0]
    assert len(schema_hash) == 64
    connection.close()


def _assert_bootstrap_recovered(path) -> None:
    repository = SqliteTaskRepository(path)
    repository.close()
    connection = sqlite3.connect(path)
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    assert "schema_meta" in names
    stored_hash = connection.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (SCHEMA_HASH_KEY,)
    ).fetchone()[0]
    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()[0] == "3"
    assert stored_hash == schema_fingerprint(connection)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_schema_bootstrap_hard_exit_mid_ddl_is_recoverable(tmp_path) -> None:
    path = tmp_path / "mid-ddl.db"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sqlite3, sys; from nasmove.persistence.schema import initialize_database; "
                "c=sqlite3.connect(sys.argv[1]); "
                "c.set_trace_callback(lambda statement: os._exit(23) if 'CREATE TABLE IF NOT EXISTS transfer_items' in statement else None); "
                "initialize_database(c)"
            ),
            str(path),
        ],
        check=False,
    )
    assert child.returncode == 23
    _assert_bootstrap_recovered(path)


def test_schema_bootstrap_hard_exit_before_metadata_is_recoverable(tmp_path) -> None:
    path = tmp_path / "before-metadata.db"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sqlite3, sys; from nasmove.persistence.schema import initialize_database; "
                "c=sqlite3.connect(sys.argv[1]); "
                "c.set_trace_callback(lambda statement: os._exit(29) if statement.startswith('INSERT INTO schema_meta') else None); "
                "initialize_database(c)"
            ),
            str(path),
        ],
        check=False,
    )
    assert child.returncode == 29
    _assert_bootstrap_recovered(path)
