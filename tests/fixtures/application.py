from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from nasmove.core.model import TaskId, TaskRecord
from nasmove.core.states import TaskState
from tests.fixtures.builders import build_task_record


class ApplicationRepository:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.tasks: dict[TaskId, TaskRecord] = {}
        self.closed = False
        self.fail_transition = False
        self.fail_close = False

    def seed(self, task: TaskRecord) -> None:
        self.tasks[task.id] = task

    def integrity_check(self) -> str:
        self.trace.append("integrity_check")
        return "ok"

    def mark_active_tasks_interrupted(self) -> int:
        self.trace.append("mark_interrupted")
        count = 0
        active = {
            TaskState.RUNNING,
            TaskState.VERIFYING,
            TaskState.COMMITTING,
            TaskState.DELETING_SOURCE,
        }
        for task_id, task in list(self.tasks.items()):
            if task.state in active:
                self.tasks[task_id] = replace(task, state=TaskState.INTERRUPTED)
                count += 1
        return count

    def list_incomplete_tasks(self) -> list[TaskRecord]:
        self.trace.append("list_incomplete")
        terminal = {
            TaskState.COMPLETED,
            TaskState.COMPLETED_WITH_WARNINGS,
            TaskState.FAILED,
            TaskState.CANCELED,
        }
        return sorted(
            (task for task in self.tasks.values() if task.state not in terminal),
            key=lambda task: (task.queue_position, str(task.id)),
        )

    def transition_task(self, task_id: TaskId, expected: TaskState, target: TaskState) -> None:
        self.trace.append(f"transition:{target.value}")
        if self.fail_transition:
            raise OSError("injected persistence failure")
        task = self.tasks[task_id]
        if task.state is not expected:
            raise RuntimeError("unexpected task state")
        self.tasks[task_id] = replace(task, state=target)

    def task_state(self, task_id: TaskId) -> TaskState:
        return self.tasks[task_id].state

    def close(self) -> None:
        self.trace.append("close_db")
        if self.fail_close:
            self.fail_close = False
            raise OSError("injected database close failure")
        self.closed = True


class ApplicationQueue:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.enqueued: list[TaskId] = []
        self.accepting = True
        self.boundary_reached = Event()
        self.flush_called = False
        self.fail_flush = False

    def enqueue(self, task_id: TaskId) -> None:
        if not self.accepting:
            raise RuntimeError("queue is stopping")
        self.trace.append(f"enqueue:{task_id}")
        self.enqueued.append(task_id)

    def stop_accepting(self) -> None:
        self.trace.append("stop_enqueue")
        self.accepting = False

    def request_pause(self) -> None:
        self.trace.append("request_pause")

    def wait_for_safe_boundary(self, timeout: float) -> bool:
        self.trace.append("wait_boundary")
        return self.boundary_reached.wait(timeout)

    def flush_and_checkpoint(self) -> None:
        self.trace.append("flush_checkpoint")
        if self.fail_flush:
            raise OSError("injected flush failure")
        self.flush_called = True


class ApplicationSmb:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.closed = False

    def disconnect(self) -> None:
        self.trace.append("disconnect_smb")
        if getattr(self, "fail_disconnect", False):
            self.fail_disconnect = False
            raise OSError("injected SMB disconnect failure")
        self.closed = True


class ApplicationCredentials:
    def __init__(self, passwords: dict[str, str | None]) -> None:
        self.passwords = passwords
        self.requested: list[str] = []

    def get_password(self, profile_id: object) -> str | None:
        profile = str(profile_id)
        self.requested.append(profile)
        return self.passwords.get(profile)


@dataclass
class ApplicationFixture:
    service: Any
    repository: ApplicationRepository
    queue: ApplicationQueue
    smb: ApplicationSmb
    credentials: ApplicationCredentials
    trace: list[str]
    task_id: TaskId
    lock_path: Path

    def seed_running_task(self) -> None:
        task = self.repository.tasks[self.task_id]
        self.repository.tasks[self.task_id] = replace(task, state=TaskState.RUNNING)


@pytest.fixture
def app_fixture(tmp_path: Path) -> ApplicationFixture:
    from nasmove.app import ApplicationService

    trace: list[str] = []
    repository = ApplicationRepository(trace)
    task = replace(
        build_task_record(), id=TaskId("application-task"), state=TaskState.QUEUED, queue_position=1
    )
    repository.seed(task)
    queue = ApplicationQueue(trace)
    smb = ApplicationSmb(trace)
    credentials = ApplicationCredentials({str(task.connection.profile_id): "secret"})
    lock_path = tmp_path / "Library" / "Application Support" / "NasMove" / "app.lock"
    service = ApplicationService(
        repository=repository,
        queue=queue,
        smb_gateway=smb,
        credential_store=credentials,
        lock_path=lock_path,
        transfer_ownership=True,
    )
    return ApplicationFixture(
        service, repository, queue, smb, credentials, trace, task.id, lock_path
    )
