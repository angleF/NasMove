import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nasmove.core.errors import ConcurrentStateChange, InvalidTransition
from nasmove.core.model import Checkpoint, ConnectionProfileId, RemotePath, TaskId, TransferItemId
from nasmove.core.states import ItemState, TaskState, TransferAction
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
    assert reopened.connection.execute("SELECT count(*) FROM transfer_items").fetchone()[0] == 0
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
        assert reopened.connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert reopened.connection.execute("SELECT count(*) FROM transfer_items").fetchone()[0] == 0
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
        repository.create_task(task, [_item(f"item-{index}", task.id)])
    assert repository.next_queued_task().id == TaskId("task-1")
    assert [task.id for task in repository.list_incomplete_tasks()] == [TaskId("task-1"), TaskId("task-0")]
    repository.reorder_queued_tasks([TaskId("task-0"), TaskId("task-1")])
    assert repository.next_queued_task().id == TaskId("task-0")
    before = [row[0] for row in repository.connection.execute("SELECT task_id FROM tasks ORDER BY task_id")]
    with pytest.raises(ValueError):
        repository.reorder_queued_tasks([TaskId("task-0"), TaskId("task-0")])
    after = [row[0] for row in repository.connection.execute("SELECT task_id FROM tasks ORDER BY task_id")]
    assert before == after
    repository.close()


def test_mark_active_tasks_interrupted_is_atomic_and_excludes_queued(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "interrupted.db")
    active = replace(build_task_record(), id=TaskId("active"), state=TaskState.RUNNING)
    queued = replace(build_task_record(), id=TaskId("queued"), state=TaskState.QUEUED)
    repository.create_task(active, [_item("active-item", active.id)])
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
    with pytest.raises(sqlite3.IntegrityError):
        repository.connection.execute(
            "INSERT INTO transfer_items(item_id, task_id, source_path, relative_path, final_path, temp_path, "
            "source_device, source_inode, source_kind, source_size, source_mtime_ns, state, confirmed_offset, "
            "retry_count, full_hash_verified, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("other", "missing-task", "/x", "x", "target/x", "target/.x", 1, 1, "file", 0, 0, "planned", 0, 0, 0, 0),
        )
    repository.connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        repository.create_task(task, [])
    repository.close()
