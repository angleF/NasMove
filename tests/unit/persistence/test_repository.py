import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nasmove.core.errors import ConcurrentStateChange, ConnectionProfileInUse, InvalidTransition
from nasmove.core.model import Checkpoint, ConnectionProfileId, RemotePath, TaskId, TransferItemId
from nasmove.core.states import ConflictPolicy, ItemState, TaskState, TransferAction
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from tests.fixtures.builders import build_task_record, build_transfer_item_record


def _item(item_id: str, task_id: TaskId, *, size: int = 128, state: ItemState = ItemState.PLANNED):
    base = build_transfer_item_record()
    return replace(
        base,
        id=TransferItemId(item_id),
        task_id=task_id,
        source_path=Path(f"/source/{item_id}.bin"),
        source_fingerprint=replace(base.source_fingerprint, size=size),
        state=state,
    )


def test_checkpoint_and_item_offset_commit_atomically(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    task = build_task_record()
    item = _item("item-atomic", task.id)
    repository.create_task(task, [item])
    repository.save_checkpoint(Checkpoint(item.id, 64, 128, 60, 4, "a" * 64, 1))
    assert repository.get_item(item.id).confirmed_offset == 64
    assert repository.checkpoints_desc(item.id)[0].confirmed_offset == 64
    repository.close()


def test_task_and_item_round_trip_preserves_all_fields(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "roundtrip.db")
    now = datetime(2025, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
    original = build_task_record()
    task = replace(
        original,
        name="Move everything",
        action=TransferAction.MOVE,
        state=TaskState.QUEUED,
        queue_position=7,
        recovery_generation=9,
        total_files=3,
        total_bytes=128,
        copied_bytes=64,
        verified_bytes=32,
        revision=4,
        created_at=now,
        updated_at=now,
        connection=replace(
            original.connection,
            profile_id=ConnectionProfileId("profile-x"),
            domain="WORKGROUP",
            require_encryption=False,
            minimum_dialect="3.1.1",
        ),
        target_root=RemotePath("archive/2025"),
    )
    item = replace(
        _item("item-x", task.id),
        confirmed_offset=64,
        retry_count=2,
        sha256="b" * 64,
        full_hash_verified=True,
        target_file_id="fid",
        final_size=128,
        committed_at=now,
        verified_session_generation=9,
        revision=3,
    )
    repository.create_task(task, [item])
    assert repository.get_task(task.id) == task
    assert repository.get_item(item.id) == item
    repository.close()


@pytest.mark.parametrize("policy", list(ConflictPolicy))
def test_task_round_trip_preserves_conflict_strategy(
    tmp_path, policy: ConflictPolicy
) -> None:
    repository = SqliteTaskRepository(tmp_path / f"{policy.value}.db")
    task = replace(
        build_task_record(),
        id=TaskId(f"task-{policy.value}"),
        conflict_policy=policy,
    )

    repository.create_task(task, [])

    assert repository.get_task(task.id).conflict_policy is policy
    repository.close()


def test_last_successful_connection_round_trips_without_a_task(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "profiles.db")
    config = replace(
        build_task_record().connection,
        display_name="家庭 NAS",
        host="nas.home",
        share="迁移",
        username="operator",
    )

    assert repository.last_successful_connection() is None
    repository.save_successful_connection(config)

    assert repository.last_successful_connection() == config
    repository.close()


def test_task_uses_parallel_item_snapshot_after_profile_update(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "parallel-item-snapshot.db")
    profile = replace(build_task_record().connection, max_parallel_items=4)
    task = build_task_record(connection=profile)
    repository.create_task(task, [build_transfer_item_record()])
    repository.save_successful_connection(replace(profile, max_parallel_items=1))

    assert repository.get_connection_profile(profile.profile_id).max_parallel_items == 1
    assert repository.get_task(task.id).connection.max_parallel_items == 4
    repository.close()


def test_connection_profiles_list_get_archive_and_reactivate(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "profile-lifecycle.db")
    original = replace(
        build_task_record().connection,
        profile_id=ConnectionProfileId("home-nas"),
        display_name="家庭 NAS",
        host="nas.home",
    )
    other = replace(
        original,
        profile_id=ConnectionProfileId("office-nas"),
        display_name="办公 NAS",
        host="nas.office",
    )
    repository.save_successful_connection(original)
    repository.save_successful_connection(other)

    assert repository.get_connection_profile(original.profile_id) == original
    assert repository.list_connection_profiles() == (other, original)

    repository.archive_connection_profile(original.profile_id)

    assert repository.list_connection_profiles() == (other,)
    with pytest.raises(KeyError):
        repository.get_connection_profile(original.profile_id)

    reactivated = replace(original, display_name="家庭存储")
    repository.save_successful_connection(reactivated)
    assert repository.get_connection_profile(original.profile_id) == reactivated
    assert repository.list_connection_profiles() == (reactivated, other)
    repository.close()


def test_connection_profile_archive_is_blocked_by_incomplete_task(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "active-profile.db")
    task = build_task_record()
    repository.create_task(task, [build_transfer_item_record()])

    with pytest.raises(ConnectionProfileInUse):
        repository.archive_connection_profile(task.connection.profile_id)

    assert repository.get_connection_profile(task.connection.profile_id) == task.connection
    repository.close()


def test_connection_profile_archive_preserves_terminal_task_history(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "terminal-profile.db")
    task = build_task_record()
    repository.create_task(task, [build_transfer_item_record()])
    repository.transition_task(task.id, TaskState.DRAFT, TaskState.CANCELED)

    repository.archive_connection_profile(task.connection.profile_id)

    assert repository.get_task(task.id).connection == task.connection
    assert repository.get_item(build_transfer_item_record().id).task_id == task.id
    with pytest.raises(KeyError):
        repository.get_connection_profile(task.connection.profile_id)
    repository.close()


def test_list_items_returns_planning_order_deterministically(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "items.db")
    task = build_task_record()
    first = _item("item-b", task.id)
    second = _item("item-a", task.id)
    repository.create_task(task, [first, second])

    assert [item.id for item in repository.list_items(task.id)] == [TransferItemId("item-a"), TransferItemId("item-b")]
    repository.close()


def test_create_task_consumes_iterable_in_batches_and_rolls_back_iteration_failure(tmp_path) -> None:
    path = tmp_path / "iter.db"
    repository = SqliteTaskRepository(path)
    task = build_task_record()

    def items():
        for index in range(1001):
            yield _item(f"item-{index}", task.id)
        raise RuntimeError("planner failed")

    with pytest.raises(RuntimeError):
        repository.create_task(task, items())
    repository.close()
    reopened = SqliteTaskRepository(path)
    with pytest.raises(KeyError):
        reopened.get_task(task.id)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT count(*) FROM transfer_items").fetchone()[0] == 0
    raw.close()
    reopened.close()


def test_create_task_rolls_back_insert_and_commit_failures(tmp_path, monkeypatch) -> None:
    for suffix, fault in (("insert", "insert"), ("commit", "commit")):
        path = tmp_path / f"{suffix}.db"
        repository = SqliteTaskRepository(path)
        task = replace(build_task_record(), id=TaskId(f"task-{suffix}"))
        if fault == "insert":
            def fail_insert(_items):
                raise OSError("disk full")

            monkeypatch.setattr(repository, "_insert_item_batch", fail_insert)
        else:
            def fail_commit():
                raise OSError("commit failed")

            monkeypatch.setattr(repository, "_commit", fail_commit)
        with pytest.raises(OSError):
            repository.create_task(task, [_item(f"item-{suffix}", task.id)])
        repository.close()
        reopened = SqliteTaskRepository(path)
        raw = sqlite3.connect(path)
        assert raw.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert raw.execute("SELECT count(*) FROM transfer_items").fetchone()[0] == 0
        raw.close()
        reopened.close()


def test_state_transitions_are_validated_and_compare_and_swap(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "cas.db")
    task = build_task_record()
    item = build_transfer_item_record()
    repository.create_task(task, [item])
    repository.transition_task(task.id, TaskState.DRAFT, TaskState.PREFLIGHT)
    assert repository.get_task(task.id).state is TaskState.PREFLIGHT
    with pytest.raises(ConcurrentStateChange):
        repository.transition_task(task.id, TaskState.DRAFT, TaskState.PREFLIGHT)
    with pytest.raises(InvalidTransition):
        repository.transition_item(item.id, ItemState.PLANNED, ItemState.VERIFIED)
    with pytest.raises(ConcurrentStateChange):
        repository.transition_item(item.id, ItemState.TRANSFERRING, ItemState.TRANSFERRED)
    repository.close()


def test_metadata_update_only_changes_approved_fields_and_revision(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "metadata.db")
    task = build_task_record()
    item = build_transfer_item_record()
    repository.create_task(task, [item])
    updated = replace(item, final_path=RemotePath("target/new.bin"), sha256="c" * 64, final_size=1024)
    repository.update_item_metadata(updated, expected_revision=0)
    assert repository.get_item(item.id).final_path == RemotePath("target/new.bin")
    assert repository.get_item(item.id).revision == 1
    with pytest.raises(ConcurrentStateChange):
        repository.update_item_metadata(replace(updated, final_size=1), expected_revision=0)
    with pytest.raises(ConcurrentStateChange):
        repository.update_item_metadata(replace(updated, state=ItemState.INTERRUPTED), expected_revision=1)
    repository.close()


def test_checkpoint_rejects_rollback_and_source_overflow(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "checkpoint.db")
    task = build_task_record()
    item = _item("item-cp", task.id)
    repository.create_task(task, [item])
    repository.save_checkpoint(Checkpoint(item.id, 64, 128, 60, 4, "a" * 64, 1))
    with pytest.raises(ValueError):
        repository.save_checkpoint(Checkpoint(item.id, 32, 128, 30, 2, "b" * 64, 1))
    with pytest.raises(ValueError):
        repository.save_checkpoint(Checkpoint(item.id, 128, 129, 120, 8, "c" * 64, 1))
    assert len(repository.checkpoints_desc(item.id)) == 1
    repository.close()


def test_checkpoint_fault_windows_rollback_both_writes(tmp_path, monkeypatch) -> None:
    for suffix, hook in (
        ("insert", "_insert_checkpoint"),
        ("update", "_update_item_checkpoint_offset"),
        ("commit", "_commit"),
    ):
        path = tmp_path / f"cp-{suffix}.db"
        repository = SqliteTaskRepository(path)
        task = replace(build_task_record(), id=TaskId(f"task-{suffix}"))
        item = _item(f"item-{suffix}", task.id)
        repository.create_task(task, [item])

        def fail(*_args, error=suffix):
            raise OSError(error)

        monkeypatch.setattr(repository, hook, fail)
        with pytest.raises(OSError):
            repository.save_checkpoint(Checkpoint(item.id, 64, 128, 60, 4, "a" * 64, 1))
        repository.close()
        reopened = SqliteTaskRepository(path)
        assert reopened.get_item(item.id).confirmed_offset == 0
        assert reopened.checkpoints_desc(item.id) == []
        reopened.close()


def test_queue_order_and_incomplete_filter_are_deterministic(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "queue.db")
    tasks = [
        replace(
            build_task_record(),
            id=TaskId(f"task-{index}"),
            state=state,
            queue_position=position,
        )
        for index, (state, position) in enumerate(
            ((TaskState.QUEUED, 2), (TaskState.QUEUED, 1), (TaskState.COMPLETED, 0))
        )
    ]
    for index, task in enumerate(tasks):
        requested_state = task.state
        task = replace(task, state=TaskState.DRAFT)
        repository.create_task(task, [_item(f"item-{index}", task.id)])
        if requested_state is TaskState.QUEUED:
            repository.transition_task(task.id, TaskState.DRAFT, TaskState.PREFLIGHT)
            repository.transition_task(task.id, TaskState.PREFLIGHT, TaskState.QUEUED)
        elif requested_state is TaskState.COMPLETED:
            repository.transition_task(task.id, TaskState.DRAFT, TaskState.PREFLIGHT)
            repository.transition_task(task.id, TaskState.PREFLIGHT, TaskState.QUEUED)
            repository.transition_task(task.id, TaskState.QUEUED, TaskState.CANCELED)
    assert repository.next_queued_task().id == TaskId("task-1")
    assert [task.id for task in repository.list_incomplete_tasks()] == [TaskId("task-1"), TaskId("task-0")]
    repository.reorder_queued_tasks([TaskId("task-0"), TaskId("task-1")])
    assert repository.next_queued_task().id == TaskId("task-0")
    raw = sqlite3.connect(tmp_path / "queue.db")
    before = [row[0] for row in raw.execute("SELECT task_id FROM tasks ORDER BY task_id")]
    with pytest.raises(ValueError):
        repository.reorder_queued_tasks([TaskId("task-0"), TaskId("task-0")])
    after = [row[0] for row in raw.execute("SELECT task_id FROM tasks ORDER BY task_id")]
    assert before == after
    raw.close()
    repository.close()


def test_mark_active_tasks_interrupted_is_atomic_and_excludes_queued(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "interrupted.db")
    active = replace(build_task_record(), id=TaskId("active"), state=TaskState.RUNNING)
    queued = replace(build_task_record(), id=TaskId("queued"), state=TaskState.QUEUED)
    repository.create_task(replace(active, state=TaskState.DRAFT), [_item("active-item", active.id)])
    repository.transition_task(active.id, TaskState.DRAFT, TaskState.PREFLIGHT)
    repository.transition_task(active.id, TaskState.PREFLIGHT, TaskState.QUEUED)
    repository.transition_task(active.id, TaskState.QUEUED, TaskState.RUNNING)
    repository.create_task(queued, [_item("queued-item", queued.id)])
    assert repository.mark_active_tasks_interrupted() == 1
    assert repository.get_task(active.id).state is TaskState.INTERRUPTED
    assert repository.get_task(queued.id).state is TaskState.QUEUED
    repository.close()


def test_foreign_keys_and_unique_ids_are_enforced(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "constraints.db")
    task = build_task_record()
    item = build_transfer_item_record()
    repository.create_task(task, [item])
    raw = sqlite3.connect(tmp_path / "constraints.db")
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
            "INSERT INTO transfer_items(item_id, task_id, source_path, relative_path, final_path, temp_path, "
            "source_device, source_inode, source_kind, source_size, source_mtime_ns, state, confirmed_offset, "
            "retry_count, full_hash_verified, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("other", "missing-task", "/x", "x", "target/x", "target/.x", 1, 1, "file", 0, 0, "planned", 0, 0, 0, 0),
        )
    finally:
        raw.rollback()
        raw.close()
    with pytest.raises(sqlite3.IntegrityError):
        repository.create_task(task, [])
    repository.close()


def test_restart_increments_recovery_and_revokes_old_verification(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "recovery.db")
    active = replace(build_task_record(), id=TaskId("active"), state=TaskState.RUNNING)
    paused = replace(build_task_record(), id=TaskId("paused"), state=TaskState.PAUSED)
    waiting = replace(build_task_record(), id=TaskId("waiting"), state=TaskState.WAITING_FOR_NETWORK)
    queued = replace(build_task_record(), id=TaskId("queued"), state=TaskState.QUEUED)
    terminal = replace(build_task_record(), id=TaskId("terminal"), state=TaskState.COMPLETED)
    for index, task in enumerate((active, paused, waiting, queued, terminal)):
        item = replace(
            _item(f"item-{index}", task.id),
            full_hash_verified=True,
            sha256="a" * 64,
            verified_session_generation=1,
        )
        seed = replace(task, state=TaskState.DRAFT)
        repository.create_task(seed, [item])
        if task.state is TaskState.RUNNING:
            repository.transition_task(task.id, TaskState.DRAFT, TaskState.PREFLIGHT)
            repository.transition_task(task.id, TaskState.PREFLIGHT, TaskState.QUEUED)
            repository.transition_task(task.id, TaskState.QUEUED, TaskState.RUNNING)
        elif task.state is TaskState.PAUSED:
            repository.transition_task(task.id, TaskState.DRAFT, TaskState.PREFLIGHT)
            repository.transition_task(task.id, TaskState.PREFLIGHT, TaskState.QUEUED)
            repository.transition_task(task.id, TaskState.QUEUED, TaskState.PAUSED)
        elif task.state is TaskState.WAITING_FOR_NETWORK:
            repository.transition_task(task.id, TaskState.DRAFT, TaskState.PREFLIGHT)
            repository.transition_task(task.id, TaskState.PREFLIGHT, TaskState.QUEUED)
            repository.transition_task(task.id, TaskState.QUEUED, TaskState.RUNNING)
            repository.transition_task(task.id, TaskState.RUNNING, TaskState.WAITING_FOR_NETWORK)
        elif task.state is TaskState.COMPLETED:
            repository.transition_task(task.id, TaskState.DRAFT, TaskState.PREFLIGHT)
            repository.transition_task(task.id, TaskState.PREFLIGHT, TaskState.QUEUED)
            repository.transition_task(task.id, TaskState.QUEUED, TaskState.RUNNING)
            repository.transition_task(task.id, TaskState.RUNNING, TaskState.VERIFYING)
            repository.transition_task(task.id, TaskState.VERIFYING, TaskState.COMMITTING)
            repository.transition_task(task.id, TaskState.COMMITTING, TaskState.COMPLETED)
    assert repository.mark_active_tasks_interrupted() == 1
    assert repository.get_task(active.id).state is TaskState.INTERRUPTED
    for task in (active, paused, waiting, queued):
        assert repository.get_task(task.id).recovery_generation == task.recovery_generation + 1
        item = repository.get_item(TransferItemId(f"item-{(active, paused, waiting, queued).index(task)}"))
        assert item.full_hash_verified is False
        assert item.verified_session_generation is None
        assert item.revision == 1
    assert repository.get_task(terminal.id).recovery_generation == terminal.recovery_generation
    assert repository.get_item(TransferItemId("item-4")).full_hash_verified is True
    repository.close()


def test_repository_rejects_non_initial_states_and_raw_state_bypass(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "guards.db")
    invalid_task = replace(build_task_record(), state=TaskState.RUNNING)
    with pytest.raises(ValueError):
        repository.create_task(invalid_task, [])
    invalid_item_task = build_task_record()
    invalid_item = _item("invalid", invalid_item_task.id, state=ItemState.TRANSFERRING)
    with pytest.raises(ValueError):
        repository.create_task(invalid_item_task, [invalid_item])
    repository.create_task(build_task_record(), [build_transfer_item_record()])
    raw = sqlite3.connect(tmp_path / "guards.db")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE transfer_items SET state = 'done' WHERE item_id = 'item-1'")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE transfer_items SET source_size = -1 WHERE item_id = 'item-1'")
    raw.rollback()
    raw.close()
    repository.close()


def test_database_transition_trigger_allows_core_edge_and_rejects_bypass(tmp_path) -> None:
    path = tmp_path / "trigger.db"
    repository = SqliteTaskRepository(path)
    repository.create_task(build_task_record(), [build_transfer_item_record()])
    raw = sqlite3.connect(path)
    raw.execute("UPDATE transfer_items SET state = 'transferring' WHERE item_id = 'item-1'")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE transfer_items SET state = 'done' WHERE item_id = 'item-1'")
    raw.rollback()
    raw.close()
    repository.close()


def test_mark_active_recovery_failure_rolls_back_generation_and_evidence(tmp_path, monkeypatch) -> None:
    path = tmp_path / "recovery-fault.db"
    repository = SqliteTaskRepository(path)
    task = replace(build_task_record(), id=TaskId("recover"), state=TaskState.DRAFT)
    item = replace(
        _item("recover-item", task.id),
        full_hash_verified=True,
        sha256="a" * 64,
        verified_session_generation=1,
    )
    repository.create_task(task, [item])
    repository.transition_task(task.id, TaskState.DRAFT, TaskState.PREFLIGHT)
    repository.transition_task(task.id, TaskState.PREFLIGHT, TaskState.QUEUED)
    repository.transition_task(task.id, TaskState.QUEUED, TaskState.RUNNING)

    def fail_commit():
        raise OSError("crash before recovery commit")

    monkeypatch.setattr(repository, "_commit", fail_commit)
    with pytest.raises(OSError):
        repository.mark_active_tasks_interrupted()
    repository.close()
    reopened = SqliteTaskRepository(path)
    assert reopened.get_task(task.id).state is TaskState.RUNNING
    assert reopened.get_task(task.id).recovery_generation == 1
    assert reopened.get_item(item.id).full_hash_verified is True
    assert reopened.get_item(item.id).verified_session_generation == 1
    reopened.close()


def test_permission_hardening_failure_happens_before_create_commit(tmp_path, monkeypatch) -> None:
    path = tmp_path / "permission-create.db"
    repository = SqliteTaskRepository(path)

    def fail_hardening():
        raise PermissionError("injected permission failure")

    monkeypatch.setattr(repository, "_harden_database_permissions", fail_hardening)
    with pytest.raises(PermissionError):
        repository.create_task(build_task_record(), [build_transfer_item_record()])
    repository.close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert raw.execute("SELECT count(*) FROM transfer_items").fetchone()[0] == 0
    raw.close()


def test_permission_hardening_failure_happens_before_checkpoint_commit(tmp_path, monkeypatch) -> None:
    path = tmp_path / "permission-checkpoint.db"
    repository = SqliteTaskRepository(path)
    task = build_task_record()
    item = _item("permission-item", task.id)
    repository.create_task(task, [item])

    def fail_hardening():
        raise PermissionError("injected permission failure")

    monkeypatch.setattr(repository, "_harden_database_permissions", fail_hardening)
    with pytest.raises(PermissionError):
        repository.save_checkpoint(Checkpoint(item.id, 64, 128, 60, 4, "a" * 64, 1))
    repository.close()
    reopened = SqliteTaskRepository(path)
    assert reopened.get_item(item.id).confirmed_offset == 0
    assert reopened.checkpoints_desc(item.id) == []
    reopened.close()


@pytest.mark.parametrize("version", ["1", "2"])
def test_old_schema_version_is_rejected(tmp_path, version: str) -> None:
    path = tmp_path / f"old-{version}.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute("INSERT INTO schema_meta VALUES ('version', ?)", (version,))
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError):
        SqliteTaskRepository(path)


@pytest.mark.parametrize("kind", ["table", "view", "index", "trigger"])
def test_database_with_objects_but_missing_schema_version_is_rejected(tmp_path, kind: str) -> None:
    path = tmp_path / f"legacy-{kind}.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE legacy_data (id INTEGER PRIMARY KEY, value TEXT)")
    raw.execute("INSERT INTO legacy_data(value) VALUES ('keep')")
    if kind == "view":
        raw.execute("CREATE VIEW legacy_view AS SELECT * FROM legacy_data")
    elif kind == "index":
        raw.execute("CREATE INDEX legacy_index ON legacy_data(value)")
    elif kind == "trigger":
        raw.execute("CREATE TRIGGER legacy_trigger AFTER INSERT ON legacy_data BEGIN SELECT 1; END")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError):
        SqliteTaskRepository(path)
    check = sqlite3.connect(path)
    assert check.execute("SELECT value FROM legacy_data").fetchone()[0] == "keep"
    assert check.execute(
        "SELECT count(*) FROM sqlite_master WHERE name = 'schema_meta'"
    ).fetchone()[0] == 0
    check.close()


def test_new_database_directory_and_file_are_private(tmp_path) -> None:
    database_dir = tmp_path / "state"
    database_path = database_dir / "nasmove.db"
    repository = SqliteTaskRepository(database_path)
    repository.close()
    assert stat.S_IMODE(os.stat(database_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(database_path).st_mode) == 0o600


def test_existing_wide_database_directory_is_rejected_without_chmod(tmp_path) -> None:
    database_dir = tmp_path / "Documents"
    database_dir.mkdir(mode=0o755)
    before = stat.S_IMODE(os.stat(database_dir).st_mode)
    with pytest.raises(PermissionError):
        SqliteTaskRepository(database_dir / "nasmove.db")
    assert stat.S_IMODE(os.stat(database_dir).st_mode) == before


def test_existing_private_database_directory_is_accepted(tmp_path) -> None:
    database_dir = tmp_path / "state"
    database_dir.mkdir(mode=0o700)
    repository = SqliteTaskRepository(database_dir / "nasmove.db")
    repository.close()
    assert stat.S_IMODE(os.stat(database_dir).st_mode) == 0o700


def test_symlink_parent_and_database_are_rejected(tmp_path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    symlink_dir = tmp_path / "link"
    symlink_dir.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(ValueError):
        SqliteTaskRepository(symlink_dir / "nasmove.db")
    symlink_db = tmp_path / "db-link"
    target = tmp_path / "target.db"
    target.touch()
    symlink_db.symlink_to(target)
    with pytest.raises(ValueError):
        SqliteTaskRepository(symlink_db)


def test_non_directory_parent_and_non_regular_database_are_rejected(tmp_path) -> None:
    parent_file = tmp_path / "parent"
    parent_file.write_text("x")
    with pytest.raises(ValueError):
        SqliteTaskRepository(parent_file / "nasmove.db")
    directory_target = tmp_path / "directory.db"
    directory_target.mkdir()
    with pytest.raises(ValueError):
        SqliteTaskRepository(directory_target)


def test_unknown_existing_database_is_rejected_without_mutation(tmp_path) -> None:
    database_dir = tmp_path / "state"
    database_dir.mkdir(mode=0o700)
    path = database_dir / "unknown.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE unrelated (value TEXT)")
    raw.execute("INSERT INTO unrelated VALUES ('preserve')")
    raw.commit()
    raw.close()
    os.chmod(path, 0o640)
    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    wal.touch(mode=0o640)
    shm.touch(mode=0o640)
    before_dir_mode = stat.S_IMODE(os.stat(database_dir).st_mode)
    before_mode = stat.S_IMODE(os.stat(path).st_mode)
    before_wal_mode = stat.S_IMODE(os.stat(wal).st_mode)
    before_shm_mode = stat.S_IMODE(os.stat(shm).st_mode)
    with pytest.raises(RuntimeError):
        SqliteTaskRepository(path)
    assert stat.S_IMODE(os.stat(database_dir).st_mode) == before_dir_mode
    assert stat.S_IMODE(os.stat(path).st_mode) == before_mode
    assert stat.S_IMODE(os.stat(wal).st_mode) == before_wal_mode
    assert stat.S_IMODE(os.stat(shm).st_mode) == before_shm_mode
    check = sqlite3.connect(path)
    assert check.execute("SELECT value FROM unrelated").fetchone()[0] == "preserve"
    assert check.execute("SELECT count(*) FROM sqlite_master WHERE name = 'schema_meta'").fetchone()[0] == 0
    check.close()


def test_empty_existing_database_is_initialized(tmp_path) -> None:
    database_dir = tmp_path / "state"
    database_dir.mkdir(mode=0o700)
    path = database_dir / "empty.db"
    path.touch()
    repository = SqliteTaskRepository(path)
    repository.close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()[0] == "7"
    assert len(raw.execute("SELECT value FROM schema_meta WHERE key = 'schema_hash'").fetchone()[0]) == 64
    raw.close()


def test_unknown_database_probe_does_not_create_sidecars(tmp_path) -> None:
    database_dir = tmp_path / "state"
    database_dir.mkdir(mode=0o700)
    path = database_dir / "unknown.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE unrelated (value TEXT)")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError):
        SqliteTaskRepository(path)
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_unknown_wal_database_is_copied_before_readonly_probe(tmp_path) -> None:
    database_dir = tmp_path / "state"
    database_dir.mkdir(mode=0o700)
    path = database_dir / "unknown-wal.db"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sqlite3, sys; c=sqlite3.connect(sys.argv[1]); "
                "c.execute('PRAGMA journal_mode=WAL'); c.execute('CREATE TABLE legacy(value TEXT)'); "
                "c.execute(\"INSERT INTO legacy VALUES ('preserve')\"); c.commit(); os._exit(17)"
            ),
            str(path),
        ],
        check=False,
    )
    assert child.returncode == 17
    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    assert wal.exists() and shm.exists()
    shm.unlink()
    before = {
        candidate: (hashlib.sha256(candidate.read_bytes()).digest(), stat.S_IMODE(os.stat(candidate).st_mode))
        for candidate in (path, wal)
    }
    with pytest.raises(RuntimeError):
        SqliteTaskRepository(path)
    for candidate, (digest, mode) in before.items():
        assert hashlib.sha256(candidate.read_bytes()).digest() == digest
        assert stat.S_IMODE(os.stat(candidate).st_mode) == mode
    assert not shm.exists()


def test_valid_v3_database_with_crash_wal_is_recovered_from_copy(tmp_path) -> None:
    path = tmp_path / "valid-wal.db"
    repository = SqliteTaskRepository(path)
    repository.create_task(build_task_record(), [build_transfer_item_record()])
    repository.close()
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sqlite3, sys; c=sqlite3.connect(sys.argv[1]); "
                "c.execute(\"INSERT INTO events(task_id,event_type,occurred_at) VALUES ('task-1','crash','2025-01-01T00:00:00+00:00')\"); "
                "c.commit(); os._exit(19)"
            ),
            str(path),
        ],
        check=False,
    )
    assert child.returncode == 19
    reopened = SqliteTaskRepository(path)
    reopened.close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT count(*) FROM events WHERE event_type = 'crash'").fetchone()[0] == 1
    raw.close()


@pytest.mark.parametrize(
    ("object_name", "needle"),
    [
        ("tasks", "name TEXT NOT NULL"),
        ("transfer_items", "REFERENCES tasks(task_id) ON DELETE CASCADE"),
        ("idx_tasks_queue", "CREATE INDEX idx_tasks_queue"),
        ("tasks_state_guard", "RAISE(ABORT"),
    ],
)
def test_schema_identity_rejects_structure_tampering(tmp_path, object_name: str, needle: str) -> None:
    path = tmp_path / f"tampered-{object_name}.db"
    repository = SqliteTaskRepository(path)
    repository.close()
    raw = sqlite3.connect(path)
    sql = raw.execute("SELECT sql FROM sqlite_master WHERE name = ?", (object_name,)).fetchone()[0]
    assert needle in sql
    raw.execute("PRAGMA writable_schema = ON")
    raw.execute(
        "UPDATE sqlite_master SET sql = ? WHERE name = ?",
        (sql.replace(needle, needle + " /* tampered */", 1), object_name),
    )
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError):
        SqliteTaskRepository(path)


def test_schema_identity_rejects_stored_hash_tampering(tmp_path) -> None:
    path = tmp_path / "tampered-hash.db"
    repository = SqliteTaskRepository(path)
    repository.close()
    raw = sqlite3.connect(path)
    raw.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_hash'", ("0" * 64,))
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError):
        SqliteTaskRepository(path)


def test_acl_on_existing_parent_is_rejected_without_mutation(tmp_path) -> None:
    database_dir = tmp_path / "acl-state"
    database_dir.mkdir(mode=0o700)
    result = subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read", str(database_dir)],
        env={"LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("macOS ACL controls are unavailable")
    before_mode = stat.S_IMODE(os.stat(database_dir).st_mode)
    before_acl = subprocess.run(
        ["/bin/ls", "-lde", str(database_dir)],
        env={"LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    try:
        with pytest.raises(PermissionError):
            SqliteTaskRepository(database_dir / "nasmove.db")
        assert stat.S_IMODE(os.stat(database_dir).st_mode) == before_mode
        after_acl = subprocess.run(
            ["/bin/ls", "-lde", str(database_dir)],
            env={"LC_ALL": "C"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert after_acl == before_acl
    finally:
        subprocess.run(
            ["/bin/chmod", "-N", str(database_dir)],
            env={"LC_ALL": "C"},
            check=False,
        )


def test_new_database_paths_have_no_acl_and_private_modes(tmp_path) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS ACL controls are unavailable")
    probe = subprocess.run(
        ["/bin/ls", "-lde", str(tmp_path)],
        env={"LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("macOS ACL controls are unavailable")
    inherited = subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read", str(tmp_path)],
        env={"LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=False,
    )
    if inherited.returncode != 0:
        pytest.skip("macOS ACL controls are unavailable")
    database_dir = tmp_path / "new-state"
    path = database_dir / "nasmove.db"
    try:
        repository = SqliteTaskRepository(path)
        repository.create_task(build_task_record(), [build_transfer_item_record()])
        repository.close()
        assert stat.S_IMODE(os.stat(database_dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        for candidate in (database_dir, path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            if not candidate.exists():
                continue
            listing = subprocess.run(
                ["/bin/ls", "-lde", str(candidate)],
                env={"LC_ALL": "C"},
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert len(listing.splitlines()) == 1
    finally:
        subprocess.run(
            ["/bin/chmod", "-N", str(tmp_path)],
            env={"LC_ALL": "C"},
            check=False,
        )


def test_deletion_outcome_is_persisted_and_readable(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    task = build_task_record()
    item = _item("item-deletion", task.id)
    repository.create_task(task, [item])

    repository.record_deletion_outcome(
        task.id,
        item.id,
        "source_retained_move_failed",
        "moving source to Trash failed: trash denied",
    )

    assert repository.last_deletion_outcome(item.id) == (
        "source_retained_move_failed",
        "moving source to Trash failed: trash denied",
    )
    repository.close()


def test_last_deletion_outcome_is_scoped_to_the_item(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    task = build_task_record()
    first = _item("item-first", task.id)
    second = _item("item-second", task.id)
    repository.create_task(task, [first, second])

    repository.record_deletion_outcome(task.id, first.id, "source_deleted", "done")
    repository.record_deletion_outcome(task.id, second.id, "source_missing_after_move", "absent")

    assert repository.last_deletion_outcome(first.id) == ("source_deleted", "done")
    assert repository.last_deletion_outcome(second.id) == ("source_missing_after_move", "absent")
    repository.close()


def test_last_deletion_outcome_for_task_is_the_newest_across_items(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    task = build_task_record()
    first = _item("item-first", task.id)
    second = _item("item-second", task.id)
    repository.create_task(task, [first, second])

    repository.record_deletion_outcome(task.id, first.id, "source_deleted", "done")
    repository.record_deletion_outcome(task.id, second.id, "deletion_refused", "refused")

    assert repository.last_deletion_outcome_for_task(task.id) == ("deletion_refused", "refused")
    assert repository.last_deletion_outcome_for_task(TaskId("task-absent")) is None
    repository.close()
