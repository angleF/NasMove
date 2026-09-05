import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest

from nasmove.core.model import Checkpoint, TaskId, TransferItemId
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from tests.fixtures.builders import build_task_record, build_transfer_item_record


@pytest.mark.parametrize("hook", ["_insert_checkpoint", "_update_item_checkpoint_offset", "_commit"])
def test_checkpoint_failure_reopens_without_half_transaction(tmp_path, monkeypatch, hook: str) -> None:
    path = tmp_path / f"{hook}.db"
    repository = SqliteTaskRepository(path)
    task = replace(build_task_record(), id=TaskId("task-fault"))
    item = replace(build_transfer_item_record(), id=TransferItemId("item-fault"), task_id=task.id)
    repository.create_task(task, [item])

    def fail(*_args):
        raise OSError("injected crash window")

    monkeypatch.setattr(repository, hook, fail)
    with pytest.raises(OSError):
        repository.save_checkpoint(Checkpoint(item.id, 64, 1024, 60, 4, "a" * 64, 1))
    repository.close()

    reopened = SqliteTaskRepository(path)
    assert reopened.get_item(item.id).confirmed_offset == 0
    assert reopened.checkpoints_desc(item.id) == []
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    raw.close()
    reopened.close()


@pytest.mark.parametrize("hook", ["insert", "update", "commit"])
def test_hard_process_exit_reopens_without_half_transaction(tmp_path, hook: str) -> None:
    path = tmp_path / f"hard-{hook}.db"
    repository = SqliteTaskRepository(path)
    task = replace(build_task_record(), id=TaskId(f"hard-{hook}"))
    item = replace(build_transfer_item_record(), id=TransferItemId(f"hard-item-{hook}"), task_id=task.id)
    repository.create_task(task, [item])
    repository.close()
    script = f'''\
import os
from nasmove.core.model import Checkpoint
from nasmove.persistence.sqlite_repository import SqliteTaskRepository

class CrashRepository(SqliteTaskRepository):
    def _insert_checkpoint(self, checkpoint):
        super()._insert_checkpoint(checkpoint)
        {"os._exit(31)" if hook == "insert" else "return None"}
    def _update_item_checkpoint_offset(self, checkpoint):
        super()._update_item_checkpoint_offset(checkpoint)
        {"os._exit(32)" if hook == "update" else "return None"}
    def _commit(self):
        {"os._exit(33)" if hook == "commit" else "super()._commit()"}

repository = CrashRepository(r"{path}")
repository.save_checkpoint(Checkpoint(r"{item.id}", 64, 1024, 60, 4, "a" * 64, 1))
'''
    result = subprocess.run([sys.executable, "-c", script], check=False, timeout=10)
    assert result.returncode in {31, 32, 33}
    reopened = SqliteTaskRepository(path)
    restored = reopened.get_item(item.id)
    assert restored.revision == 0
    assert restored.confirmed_offset == 0
    assert reopened.checkpoints_desc(item.id) == []
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    raw.close()
    reopened.close()
