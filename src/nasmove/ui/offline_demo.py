from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock
from uuid import uuid4

from PySide6.QtWidgets import QApplication

from nasmove.core.model import TaskId, TaskRecord
from nasmove.core.states import TaskState
from nasmove.planning.task_planner import PlannedTask, PlanRequest
from nasmove.transfer.progress import ProgressSnapshot
from nasmove.transfer.transfer_engine import TaskResult
from nasmove.ui.main_window import MainWindow
from nasmove.ui.view_models import ConnectionTestReport


class OfflineRepository:
    def __init__(self) -> None:
        self.tasks: dict[TaskId, TaskRecord] = {}

    def add(self, task: TaskRecord) -> None:
        self.tasks[task.id] = task

    def get_task(self, task_id: TaskId) -> TaskRecord:
        return self.tasks[task_id]

    def transition_task(self, task_id: TaskId, expected: TaskState, target: TaskState) -> None:
        task = self.tasks[task_id]
        if task.state is not expected:
            raise RuntimeError("offline task state changed")
        self.tasks[task_id] = replace(task, state=target, revision=task.revision + 1)

    def list_incomplete_tasks(self) -> list[TaskRecord]:
        return sorted(self.tasks.values(), key=lambda task: (task.queue_position, task.created_at))

    def reorder_queued_tasks(self, task_ids: tuple[TaskId, ...]) -> None:
        for position, task_id in enumerate(task_ids):
            task = self.tasks[task_id]
            self.tasks[task_id] = replace(task, queue_position=position, revision=task.revision + 1)


class OfflinePlanner:
    def __init__(self, repository: OfflineRepository) -> None:
        self._repository = repository

    def plan(self, request: PlanRequest) -> PlannedTask:
        total_bytes = sum(path.stat().st_size for path in request.sources if path.is_file())
        now = datetime.now(UTC)
        task = TaskRecord(
            id=request.task_id or TaskId(str(uuid4())),
            name=request.name,
            action=request.action,
            connection=request.connection,
            target_root=request.target_root,
            conflict_policy=request.conflict_policy,
            verification_policy=request.verification_policy,
            state=TaskState.PREFLIGHT,
            queue_position=request.queue_position,
            recovery_generation=1,
            total_files=len(request.sources),
            total_bytes=total_bytes,
            copied_bytes=0,
            verified_bytes=0,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        self._repository.add(task)
        return PlannedTask(task, len(request.sources), total_bytes, 0, total_bytes, 1 << 40)


class OfflineQueue:
    def __init__(self, repository: OfflineRepository) -> None:
        self._repository = repository
        self._task_ids: list[TaskId] = []
        self._lock = Lock()
        self._progress_sink: Callable[[ProgressSnapshot], None] | None = None

    def set_progress_sink(self, sink: Callable[[ProgressSnapshot], None]) -> None:
        self._progress_sink = sink

    def enqueue(self, task_id: TaskId) -> None:
        with self._lock:
            if task_id not in self._task_ids:
                self._task_ids.append(task_id)

    def run_next(self) -> TaskResult | None:
        with self._lock:
            if not self._task_ids:
                return None
            task_id = self._task_ids.pop(0)
        task = self._repository.get_task(task_id)
        self._repository.transition_task(task_id, task.state, TaskState.RUNNING)
        self._publish_progress(task.total_bytes, task.total_bytes, 0)
        for state in (TaskState.VERIFYING, TaskState.COMMITTING, TaskState.COMPLETED):
            current = self._repository.get_task(task_id)
            self._repository.transition_task(task_id, current.state, state)
        self._publish_progress(task.total_bytes, task.total_bytes, task.total_bytes)
        return TaskResult(True, TaskState.COMPLETED, completed_items=1)

    def _publish_progress(self, size: int, copied: int, verified: int) -> None:
        if self._progress_sink is None:
            return
        self._progress_sink(
            ProgressSnapshot(
                total_bytes=max(2, size * 2),
                completed_bytes=copied + verified,
                copied_bytes=copied,
                verified_bytes=verified,
                speed_bytes_per_second=0.0,
                eta_seconds=0.0 if verified else None,
            )
        )

    def request_pause(self) -> None:
        return

    def request_cancel(self) -> None:
        return


class OfflineApplication:
    def __init__(self, queue: OfflineQueue) -> None:
        self._queue = queue

    def enqueue(self, task_id: TaskId) -> None:
        self._queue.enqueue(task_id)


class OfflineGateway:
    def list_share_root(self) -> tuple[object, ...]:
        return ()

    def free_space_share_root(self) -> int:
        return 1 << 40

    def list_dir(self, path: object) -> tuple[object, ...]:
        del path
        return ()

    def free_space(self, path: object) -> int:
        del path
        return 1 << 40


class OfflineConnectionTester:
    def test_connection(self, request: object) -> ConnectionTestReport:
        del request
        return ConnectionTestReport.all_passed()


class InMemoryCredentialStore:
    def __init__(self) -> None:
        self._passwords: dict[str, str] = {}

    def set_password(self, profile_id: object, password: str) -> None:
        self._passwords[str(profile_id)] = password

    def get_password(self, profile_id: object) -> str | None:
        return self._passwords.get(str(profile_id))

    def delete_password(self, profile_id: object) -> None:
        self._passwords.pop(str(profile_id), None)


def build_offline_demo_window() -> MainWindow:
    repository = OfflineRepository()
    queue = OfflineQueue(repository)
    window = MainWindow(
        gateway=OfflineGateway(),
        tester=OfflineConnectionTester(),
        credential_store=InMemoryCredentialStore(),
        task_repository=repository,
        task_planner=OfflinePlanner(repository),
        application=OfflineApplication(queue),
        queue_coordinator=queue,
    )
    queue.set_progress_sink(window.task_controller.publish_progress)
    window.setWindowTitle("NasMove 离线演示（不连接 NAS）")
    return window


def main() -> int:
    application = QApplication.instance() or QApplication([])
    window = build_offline_demo_window()
    window.resize(800, 720)
    window.show()
    return int(application.exec())


__all__ = ["build_offline_demo_window", "main"]
