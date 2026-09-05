from __future__ import annotations

import sqlite3

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
