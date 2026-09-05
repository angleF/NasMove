from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from nasmove.core.model import TaskId
from nasmove.core.states import ItemState, TaskState, TransferAction


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


@pytest.mark.parametrize("window", ["write", "flush", "verify", "rename", "delete"])
def test_production_components_survive_process_exit_windows(tmp_path: Path, window: str) -> None:
    """Exercise the production writer, verifier, committer and deleter in a child process."""
    from nasmove.app import ApplicationService
    from nasmove.localio.files import PosixLocalFileGateway
    from nasmove.persistence.sqlite_repository import SqliteTaskRepository
    from tests.fixtures.builders import build_task_record, build_transfer_item_record

    path = tmp_path / "production.db"
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload" * (5 * 1024 * 1024 // 7 + 1))
    remote = tmp_path / "remote"
    remote.mkdir()
    task_id = TaskId("production-crash")
    task = replace(
        build_task_record(), id=task_id, state=TaskState.DRAFT, action=TransferAction.MOVE
    )
    task_item = replace(
        build_transfer_item_record(),
        task_id=task_id,
        source_path=source,
        source_fingerprint=PosixLocalFileGateway().fingerprint(source),
    )
    repository = SqliteTaskRepository(path)
    repository.create_task(task, [task_item])
    repository.close()

    script = r"""
import contextlib
import os
import sys
from pathlib import Path

from nasmove.core.model import TaskId
from nasmove.core.ports import RemoteStat, SessionInfo
from nasmove.core.states import ItemState, TaskState
from nasmove.localio.files import PosixLocalFileGateway
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from nasmove.transfer.checkpoint_writer import CheckpointWriter
from nasmove.transfer.commit import TargetCommitter
from nasmove.transfer.deletion import SourceDeletionService
from nasmove.transfer.verification import IntegrityVerifier

db, source_name, remote_name, window = sys.argv[1:]
remote_root = Path(remote_name)
session = SessionInfo("3.1.1", True, True, 1)

def disk_path(path):
    return remote_root / path.value.replace("/", "__")

class Stream:
    def __init__(self, raw, mode):
        self.raw = raw
        self.mode = mode
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.raw.close()
    def read(self, size=-1):
        return self.raw.read(size)
    def write(self, data):
        written = self.raw.write(data)
        self.raw.flush()
        os.fsync(self.raw.fileno())
        if window == "write" and self.mode == "write":
            os._exit(23)
        return written
    def seek(self, offset, whence=0):
        return self.raw.seek(offset, whence)
    def flush(self):
        self.raw.flush()
        os.fsync(self.raw.fileno())
        if window == "flush" and self.mode == "flush":
            os._exit(23)

class DiskSmb:
    def is_generation_current(self, generation):
        return generation == 1
    @contextlib.contextmanager
    def session_lease(self, generation):
        if generation != 1:
            raise OSError("stale session")
        yield
    def stat(self, path):
        try:
            return RemoteStat(disk_path(path).stat().st_size, False, 0, str(disk_path(path).stat().st_ino))
        except FileNotFoundError:
            return None
    def open_read(self, path):
        if window == "verify":
            os._exit(23)
        return Stream(open(disk_path(path), "rb", buffering=0), "read")
    def open_update(self, path):
        return Stream(open(disk_path(path), "r+b", buffering=0), "update")
    def create_exclusive(self, path):
        return Stream(open(disk_path(path), "xb", buffering=0), "write" if window == "write" else "flush")
    def truncate(self, path, size):
        with open(disk_path(path), "r+b", buffering=0) as stream:
            stream.truncate(size)
    def rename_exclusive(self, source_path, target_path):
        os.rename(disk_path(source_path), disk_path(target_path))
        if window == "rename":
            os._exit(23)
    def list_dir(self, path):
        return []

class ExitLocal:
    def __init__(self, base):
        self.base = base
    def fingerprint(self, path):
        return self.base.fingerprint(path)
    def open_read(self, path):
        return self.base.open_read(path)
    def remove_file(self, path, expected_fingerprint=None):
        self.base.remove_file(path, expected_fingerprint)
        if window == "delete":
            os._exit(23)
    def remove_empty_dir(self, path, expected_fingerprint=None):
        return self.base.remove_empty_dir(path, expected_fingerprint)

repository = SqliteTaskRepository(db)
task_id = TaskId("production-crash")
item = repository.get_item(repository.list_items(task_id)[0].id)
repository.transition_task(task_id, TaskState.DRAFT, TaskState.PREFLIGHT)
repository.transition_task(task_id, TaskState.PREFLIGHT, TaskState.QUEUED)
repository.transition_task(task_id, TaskState.QUEUED, TaskState.RUNNING)
repository.transition_item(item.id, ItemState.PLANNED, ItemState.TRANSFERRING)
local = PosixLocalFileGateway()
smb = DiskSmb()
writer = CheckpointWriter(repository, local, smb)
writer.copy(item, 0, session)
item = repository.get_item(item.id)
repository.transition_item(item.id, ItemState.TRANSFERRING, ItemState.TRANSFERRED)
item = repository.get_item(item.id)
repository.transition_item(item.id, ItemState.TRANSFERRED, ItemState.VERIFYING)
item = repository.get_item(item.id)
verifier = IntegrityVerifier(repository, local, smb, session)
if window == "verify":
    verifier.verify_full(item)
else:
    verification = verifier.verify_full(item)
    repository.transition_item(item.id, ItemState.VERIFYING, ItemState.VERIFIED)
    item = repository.get_item(item.id)
    if window == "rename":
        TargetCommitter(repository, smb, session).commit(item, verification)
    elif window == "delete":
        TargetCommitter(repository, smb, session).commit(item, verification)
        SourceDeletionService(repository, ExitLocal(local), smb, verifier).delete_verified_source(item.id, session)
"""
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    process = subprocess.run(
        [sys.executable, "-c", script, str(path), str(source), str(remote), window],
        env=environment,
        check=False,
    )
    assert process.returncode == 23

    queue = type("Queue", (), {"enqueue": lambda self, task_id: None})()
    reopened = SqliteTaskRepository(path)
    service = ApplicationService(
        repository=reopened,
        queue=queue,
        lock_path=tmp_path / "app.lock",
    )
    assert service.start().interrupted_tasks == 1
    item = reopened.list_items(task_id)[0]
    if window in {"write", "flush"}:
        assert item.state is ItemState.TRANSFERRING
        assert source.exists()
    elif window == "verify":
        assert item.state is ItemState.VERIFYING
        assert source.exists()
    elif window == "rename":
        assert item.state is ItemState.VERIFIED
        assert source.exists()
        assert (remote / "target__file.bin").exists()
    else:
        assert item.state is ItemState.SOURCE_DELETE_AUTHORIZED
        assert not source.exists()
        assert (remote / "target__file.bin").exists()
    service.request_shutdown()
    reopened.close()
