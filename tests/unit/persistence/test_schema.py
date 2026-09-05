import sqlite3

from nasmove.persistence.schema import SCHEMA_HASH_KEY, SCHEMA_VERSION, initialize_database


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
