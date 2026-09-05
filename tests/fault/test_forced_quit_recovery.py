from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from nasmove.core.model import TaskId
from nasmove.core.states import TaskState


def test_real_sqlite_restart_marks_running_task_interrupted(tmp_path) -> None:
    from dataclasses import replace

    from nasmove.core.model import TaskId
    from nasmove.persistence.sqlite_repository import SqliteTaskRepository
    from tests.fixtures.builders import build_task_record, build_transfer_item_record

    path = tmp_path / "nasmove.db"
    task = replace(build_task_record(), id=TaskId("crashed-task"), state=TaskState.DRAFT)
    item = replace(build_transfer_item_record(), task_id=task.id)
    repository = SqliteTaskRepository(path)
    repository.create_task(task, [item])
    repository.transition_task(task.id, TaskState.DRAFT, TaskState.PREFLIGHT)
    repository.transition_task(task.id, TaskState.PREFLIGHT, TaskState.QUEUED)
    repository.transition_task(task.id, TaskState.QUEUED, TaskState.RUNNING)
    repository.close()

    reopened = SqliteTaskRepository(path)
    assert reopened.mark_active_tasks_interrupted() == 1
    reopened.close()

    raw = sqlite3.connect(path)
    assert raw.execute("SELECT state FROM tasks WHERE task_id = ?", (str(task.id),)).fetchone()[0] == "interrupted"
    raw.close()


@pytest.mark.parametrize(
    "window, transitions",
    [
        ("write", 3),
        ("flush", 3),
        ("verify", 4),
        ("rename", 5),
        ("delete", 6),
    ],
)
def test_process_exit_at_transfer_windows_is_explainable_and_source_is_retained(
    tmp_path: Path, window: str, transitions: int
) -> None:
    from nasmove.persistence.sqlite_repository import SqliteTaskRepository
    from tests.fixtures.builders import build_task_record, build_transfer_item_record

    path = tmp_path / f"{window}.db"
    task_id = TaskId(f"crash-{window}")
    task = replace(build_task_record(), id=task_id, state=TaskState.DRAFT)
    item = replace(build_transfer_item_record(), task_id=task_id, source_path=tmp_path / "source.bin")
    source = item.source_path
    source.write_bytes(b"source remains recoverable")
    repository = SqliteTaskRepository(path)
    repository.create_task(task, [item])
    repository.close()

    script = """
import os
import sys
from nasmove.core.states import TaskState
from nasmove.core.model import TaskId
from nasmove.persistence.sqlite_repository import SqliteTaskRepository

repository = SqliteTaskRepository(sys.argv[1])
task_id = TaskId(sys.argv[2])
steps = [
    (TaskState.DRAFT, TaskState.PREFLIGHT),
    (TaskState.PREFLIGHT, TaskState.QUEUED),
    (TaskState.QUEUED, TaskState.RUNNING),
    (TaskState.RUNNING, TaskState.VERIFYING),
    (TaskState.VERIFYING, TaskState.COMMITTING),
    (TaskState.COMMITTING, TaskState.DELETING_SOURCE),
]
for current, target in steps[:int(sys.argv[3])]:
    repository.transition_task(task_id, current, target)
os._exit(23)
"""
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    process = subprocess.run(
        [sys.executable, "-c", script, str(path), str(task_id), str(transitions)],
        env=environment,
        check=False,
    )
    assert process.returncode == 23

    reopened = SqliteTaskRepository(path)
    assert reopened.get_task(task_id).state in {
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.COMMITTING,
        TaskState.DELETING_SOURCE,
    }
    assert reopened.mark_active_tasks_interrupted() == 1
    assert reopened.get_task(task_id).state is TaskState.INTERRUPTED
    reopened.close()
    assert source.read_bytes() == b"source remains recoverable"
