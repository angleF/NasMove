import sqlite3
import subprocess
import sys

import pytest

from nasmove.core.model import ConnectionProfileId
from nasmove.persistence.schema import (
    CONFLICT_STRATEGY_SQL,
    PARALLEL_ITEMS_SQL,
    PROFILE_ARCHIVE_SQL,
    SCHEMA_HASH_KEY,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    _expected_legacy_schema_fingerprint,
    _legacy_trigger_sql,
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
    ).fetchone()[0] == SCHEMA_VERSION == "8"
    profile_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(connection_profiles)")
    }
    assert "is_archived" in profile_columns
    schema_hash = connection.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (SCHEMA_HASH_KEY,)
    ).fetchone()[0]
    assert len(schema_hash) == 64
    connection.close()


def test_schema_v3_is_migrated_without_losing_connection_profiles(tmp_path) -> None:
    path = tmp_path / "schema-v3.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL + _legacy_trigger_sql())
    connection.execute(
        "INSERT INTO connection_profiles(profile_id, display_name, host, port, share, "
        "username, domain, require_encryption, minimum_dialect, keychain_account, "
        "last_test_ok, last_test_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "home-nas",
            "家庭 NAS",
            "nas.home",
            445,
            "迁移",
            "operator",
            None,
            1,
            "3.0",
            "home-nas",
            1,
            "2026-09-10T00:00:00+00:00",
        ),
    )
    legacy_hash = schema_fingerprint(connection)
    connection.execute("INSERT INTO schema_meta VALUES ('version', '3')")
    connection.execute(
        "INSERT INTO schema_meta VALUES (?, ?)", (SCHEMA_HASH_KEY, legacy_hash)
    )
    connection.commit()

    initialize_database(connection)

    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()[0] == "8"
    assert connection.execute(
        "SELECT display_name, is_archived FROM connection_profiles WHERE profile_id = ?",
        ("home-nas",),
    ).fetchone() == ("家庭 NAS", 0)
    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (SCHEMA_HASH_KEY,)
    ).fetchone()[0] == schema_fingerprint(connection)
    assert {
        row[1] for row in connection.execute("PRAGMA table_info(tasks)")
    } >= {"conflict_strategy"}
    connection.close()


def test_schema_v4_is_migrated_with_keep_both_default(tmp_path) -> None:
    path = tmp_path / "schema-v4.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL + _legacy_trigger_sql() + PROFILE_ARCHIVE_SQL)
    connection.execute("INSERT INTO schema_meta VALUES ('version', '4')")
    connection.execute(
        "INSERT INTO schema_meta VALUES (?, ?)",
        (SCHEMA_HASH_KEY, schema_fingerprint(connection)),
    )
    connection.commit()

    initialize_database(connection)

    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()[0] == "8"
    column = next(
        row for row in connection.execute("PRAGMA table_info(tasks)")
        if row[1] == "conflict_strategy"
    )
    assert column[4] == "'keep_both'"
    connection.close()


def create_exact_v5_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        SCHEMA_SQL + _legacy_trigger_sql("5") + PROFILE_ARCHIVE_SQL + CONFLICT_STRATEGY_SQL
    )
    connection.execute(
        "INSERT INTO connection_profiles(profile_id, display_name, host, port, share, "
        "username, require_encryption, minimum_dialect, keychain_account) "
        "VALUES ('v5-profile', 'V5 NAS', 'nas.v5', 445, 'share', 'user', 1, '3.0', "
        "'v5-profile')"
    )
    connection.execute(
        "INSERT INTO tasks(task_id, name, action, profile_id, connection_display_name, "
        "connection_host, connection_port, connection_share, connection_username, "
        "connection_require_encryption, connection_minimum_dialect, target_root, "
        "conflict_policy, conflict_strategy, verification_policy, state, queue_position, "
        "recovery_generation, total_files, total_bytes, copied_bytes, verified_bytes, "
        "revision, created_at, updated_at) VALUES ('v5-task', 'V5 task', 'copy', "
        "'v5-profile', 'V5 NAS', 'nas.v5', 445, 'share', 'user', 1, '3.0', 'target', "
        "'auto_rename', 'keep_both', 'full', 'draft', 0, 0, 0, 0, 0, 0, 0, "
        "'2026-09-11T00:00:00+00:00', '2026-09-11T00:00:00+00:00')"
    )
    connection.execute("INSERT INTO schema_meta VALUES ('version', '5')")
    connection.execute(
        "INSERT INTO schema_meta VALUES (?, ?)",
        (SCHEMA_HASH_KEY, schema_fingerprint(connection)),
    )
    connection.commit()
    return connection


def test_schema_v5_migrates_parallel_item_defaults(tmp_path) -> None:
    connection = create_exact_v5_database(tmp_path / "schema-v5.db")
    initialize_database(connection)
    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()[0] == "8"
    assert connection.execute(
        "SELECT max_parallel_items FROM connection_profiles"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT connection_max_parallel_items FROM tasks"
    ).fetchone()[0] == 2
    connection.close()


def test_schema_v5_migration_rejects_tampered_identity(tmp_path) -> None:
    connection = create_exact_v5_database(tmp_path / "tampered-v5.db")
    connection.execute("ALTER TABLE connection_profiles ADD COLUMN injected TEXT")
    connection.execute(
        "UPDATE schema_meta SET value = ? WHERE key = ?",
        (schema_fingerprint(connection), SCHEMA_HASH_KEY),
    )
    connection.commit()

    with pytest.raises(RuntimeError, match="unsupported schema identity: 5"):
        initialize_database(connection)
    connection.close()


def test_schema_v3_migration_rejects_tampered_identity(tmp_path) -> None:
    path = tmp_path / "tampered-v3.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL + _legacy_trigger_sql())
    connection.execute("ALTER TABLE connection_profiles ADD COLUMN injected TEXT")
    connection.execute("INSERT INTO schema_meta VALUES ('version', '3')")
    connection.execute(
        "INSERT INTO schema_meta VALUES (?, ?)",
        (SCHEMA_HASH_KEY, schema_fingerprint(connection)),
    )
    connection.commit()

    with pytest.raises(RuntimeError, match="unsupported schema identity: 3"):
        initialize_database(connection)
    connection.close()


@pytest.mark.parametrize(
    ("exit_code", "statement_prefix"),
    ((31, "ALTER TABLE connection_profiles"), (37, "INSERT INTO schema_meta")),
)
def test_schema_v3_migration_hard_exit_is_recoverable(
    tmp_path, exit_code: int, statement_prefix: str
) -> None:
    path = tmp_path / f"migration-exit-{exit_code}.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL + _legacy_trigger_sql())
    connection.execute(
        "INSERT INTO connection_profiles(profile_id, display_name, host, port, share, "
        "username, require_encryption, minimum_dialect, keychain_account, last_test_ok) "
        "VALUES ('recoverable', '可恢复 NAS', 'nas.local', 445, 'share', 'user', 1, "
        "'3.0', 'recoverable', 1)"
    )
    connection.execute("INSERT INTO schema_meta VALUES ('version', '3')")
    connection.execute(
        "INSERT INTO schema_meta VALUES (?, ?)",
        (SCHEMA_HASH_KEY, schema_fingerprint(connection)),
    )
    connection.commit()
    connection.close()

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sqlite3, sys; "
                "from nasmove.persistence.schema import initialize_database; "
                "c=sqlite3.connect(sys.argv[1]); "
                "prefix=sys.argv[2]; code=int(sys.argv[3]); "
                "c.set_trace_callback(lambda statement: os._exit(code) "
                "if statement.lstrip().startswith(prefix) else None); "
                "initialize_database(c)"
            ),
            str(path),
            statement_prefix,
            str(exit_code),
        ],
        check=False,
    )
    assert child.returncode == exit_code

    repository = SqliteTaskRepository(path)
    recovered = repository.get_connection_profile(ConnectionProfileId("recoverable"))
    assert recovered.display_name == "可恢复 NAS"
    repository.close()
    reopened = sqlite3.connect(path)
    assert reopened.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()[0] == "8"
    assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reopened.close()


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
    ).fetchone()[0] == "8"
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


# Recorded on 2026-09-12 from the shipped schema before the SQLite credential
# change.  It must never drift: every existing v6 database has this identity.
V6_SCHEMA_FINGERPRINT = "95a154ac47825d767037f48f3647816262a0ae7480cae1fbf60275b64dde2533"


def create_exact_v6_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        SCHEMA_SQL
        + _legacy_trigger_sql("6")
        + PROFILE_ARCHIVE_SQL
        + CONFLICT_STRATEGY_SQL
        + PARALLEL_ITEMS_SQL
    )
    connection.execute(
        "INSERT INTO connection_profiles(profile_id, display_name, host, port, share, "
        "username, require_encryption, minimum_dialect, keychain_account) "
        "VALUES ('v6-profile', 'V6 NAS', 'nas.v6', 445, 'share', 'user', 1, '3.0', "
        "'v6-profile')"
    )
    connection.execute("INSERT INTO schema_meta VALUES ('version', '6')")
    connection.execute(
        "INSERT INTO schema_meta VALUES (?, ?)",
        (SCHEMA_HASH_KEY, schema_fingerprint(connection)),
    )
    connection.commit()
    return connection


def test_expected_v6_fingerprint_is_frozen() -> None:
    assert _expected_legacy_schema_fingerprint("6") == V6_SCHEMA_FINGERPRINT


def test_schema_v6_migrates_to_v8_and_preserves_profiles(tmp_path) -> None:
    connection = create_exact_v6_database(tmp_path / "schema-v6.db")
    assert schema_fingerprint(connection) == V6_SCHEMA_FINGERPRINT

    initialize_database(connection)

    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()[0] == "8"
    assert connection.execute(
        "SELECT display_name, is_archived, max_parallel_items FROM connection_profiles "
        "WHERE profile_id = ?",
        ("v6-profile",),
    ).fetchone() == ("V6 NAS", 0, 2)
    assert "credentials" in {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (SCHEMA_HASH_KEY,)
    ).fetchone()[0] == schema_fingerprint(connection)
    connection.close()


def test_schema_v6_migration_rejects_tampered_identity(tmp_path) -> None:
    connection = create_exact_v6_database(tmp_path / "tampered-v6.db")
    connection.execute("ALTER TABLE connection_profiles ADD COLUMN injected TEXT")
    connection.execute(
        "UPDATE schema_meta SET value = ? WHERE key = ?",
        (schema_fingerprint(connection), SCHEMA_HASH_KEY),
    )
    connection.commit()

    with pytest.raises(RuntimeError, match="unsupported schema identity: 6"):
        initialize_database(connection)
    connection.close()


def test_credentials_table_shape_and_foreign_key(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "credentials.db")
    initialize_database(connection)
    columns = {row[1]: row for row in connection.execute("PRAGMA table_info(credentials)")}
    assert set(columns) == {"profile_id", "password"}
    assert columns["profile_id"][5] == 1  # primary key
    assert columns["password"][3] == 1  # NOT NULL
    foreign_keys = list(connection.execute("PRAGMA foreign_key_list(credentials)"))
    assert len(foreign_keys) == 1
    assert foreign_keys[0][2] == "connection_profiles"
    assert foreign_keys[0][3] == "profile_id"

    connection.execute(
        "INSERT INTO connection_profiles(profile_id, display_name, host, port, share, "
        "username, require_encryption, minimum_dialect, keychain_account) "
        "VALUES ('p1', 'One', 'nas.one', 445, 'share', 'user', 1, '3.0', 'p1')"
    )
    connection.execute("INSERT INTO credentials(profile_id, password) VALUES ('p1', 'secret')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO credentials(profile_id, password) VALUES ('p1', 'other')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO credentials(profile_id, password) VALUES ('missing', 'secret')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO credentials(profile_id, password) VALUES ('p2', NULL)")
    connection.close()


V7_SCHEMA_FINGERPRINT = "b085a0e6d762ebafb4c29ee3c4978ef8013bd7c680b7ad8d6cdc013c53e3b086"


def create_exact_v7_database(path):
    from nasmove.persistence.schema import CREDENTIALS_SQL
    connection = sqlite3.connect(path)
    connection.executescript(
        SCHEMA_SQL
        + _legacy_trigger_sql("7")
        + PROFILE_ARCHIVE_SQL
        + CONFLICT_STRATEGY_SQL
        + PARALLEL_ITEMS_SQL
        + CREDENTIALS_SQL
    )
    connection.execute(
        "INSERT INTO connection_profiles(profile_id, display_name, host, port, share, "
        "username, require_encryption, minimum_dialect, keychain_account) "
        "VALUES ('v7-profile', 'V7 NAS', 'nas.v7', 445, 'share', 'user', 1, '3.0', 'v7-profile')"
    )
    connection.execute("INSERT INTO credentials(profile_id, password) VALUES ('v7-profile', 'secret')")
    connection.execute(
        "INSERT INTO tasks(task_id, name, action, profile_id, connection_display_name, "
        "connection_host, connection_port, connection_share, connection_username, "
        "connection_require_encryption, connection_minimum_dialect, target_root, "
        "conflict_policy, conflict_strategy, verification_policy, state, queue_position, "
        "recovery_generation, total_files, total_bytes, copied_bytes, verified_bytes, "
        "revision, created_at, updated_at) VALUES ('v7-task', 'V7 task', 'copy', "
        "'v7-profile', 'V7 NAS', 'nas.v7', 445, 'share', 'user', 1, '3.0', 'target', "
        "'auto_rename', 'keep_both', 'full', 'queued', 0, 0, 0, 0, 0, 0, 0, "
        "'2026-09-12T00:00:00+00:00', '2026-09-12T00:00:00+00:00')"
    )
    connection.execute("UPDATE tasks SET state = 'running' WHERE task_id = 'v7-task'")
    connection.execute("UPDATE tasks SET state = 'failed' WHERE task_id = 'v7-task'")
    connection.execute("INSERT INTO schema_meta VALUES ('version', '7')")
    connection.execute(
        "INSERT INTO schema_meta VALUES (?, ?)",
        (SCHEMA_HASH_KEY, schema_fingerprint(connection)),
    )
    connection.commit()
    return connection


def test_expected_v7_fingerprint_is_frozen() -> None:
    assert _expected_legacy_schema_fingerprint("7") == V7_SCHEMA_FINGERPRINT


def test_schema_v7_migrates_to_v8_and_enables_failed_task_retry(tmp_path) -> None:
    connection = create_exact_v7_database(tmp_path / "schema-v7.db")
    assert schema_fingerprint(connection) == V7_SCHEMA_FINGERPRINT

    # In v7, transitioning from failed to queued raises abort from tasks_state_guard
    with pytest.raises(sqlite3.DatabaseError, match="invalid state transition"):
        connection.execute("UPDATE tasks SET state = 'queued' WHERE task_id = 'v7-task'")
    connection.rollback()

    initialize_database(connection)

    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()[0] == "8"
    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (SCHEMA_HASH_KEY,)
    ).fetchone()[0] == schema_fingerprint(connection)

    # In v8, transitioning from failed to queued succeeds
    connection.execute("UPDATE tasks SET state = 'queued' WHERE task_id = 'v7-task'")
    connection.commit()
    assert connection.execute(
        "SELECT state FROM tasks WHERE task_id = 'v7-task'"
    ).fetchone()[0] == "queued"
    connection.close()
