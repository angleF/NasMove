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
    assert reopened.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reopened.close()
