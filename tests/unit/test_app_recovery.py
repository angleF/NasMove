from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread

import pytest

from nasmove.core.states import TaskState


def test_running_tasks_become_interrupted_before_recovery(app_fixture) -> None:
    app_fixture.seed_running_task()
    report = app_fixture.service.start()

    assert report.interrupted_tasks == 1
    assert app_fixture.repository.task_state(app_fixture.task_id) is TaskState.INTERRUPTED
    assert app_fixture.trace.index("mark_interrupted") < app_fixture.trace.index("list_incomplete")


def test_start_loads_incomplete_tasks_in_queue_order(app_fixture) -> None:
    task = app_fixture.repository.tasks[app_fixture.task_id]
    second = replace(task, id=type(task.id)("second"), queue_position=0)
    app_fixture.repository.seed(second)

    report = app_fixture.service.start()

    assert report.queued_tasks == 2
    assert app_fixture.queue.enqueued == [second.id, task.id]


def test_missing_keychain_credential_pauses_task_without_exposing_password(app_fixture) -> None:
    task = app_fixture.repository.tasks[app_fixture.task_id]
    app_fixture.repository.tasks[task.id] = replace(task, state=TaskState.QUEUED)
    app_fixture.credentials.passwords.clear()

    report = app_fixture.service.start()

    assert report.paused_tasks == 1
    assert app_fixture.repository.task_state(task.id) is TaskState.PAUSED
    assert "secret" not in repr(report)
    assert app_fixture.queue.enqueued == []


def test_second_application_instance_is_reported_without_opening_database(app_fixture) -> None:
    first = app_fixture.service
    first.start()
    from nasmove.app import ApplicationService

    second_repository = type(app_fixture.repository)([])
    second = ApplicationService(
        repository=second_repository,
        queue=app_fixture.queue,
        smb_gateway=app_fixture.smb,
        credential_store=app_fixture.credentials,
        lock_path=app_fixture.lock_path,
    )

    report = second.start()

    assert report.already_running is True
    assert report.started is False
    assert second_repository.trace == []


def test_shutdown_waits_for_boundary_flushes_pauses_then_closes_in_order(app_fixture) -> None:
    app_fixture.service.start()
    app_fixture.queue.boundary_reached.set()

    result = app_fixture.service.request_shutdown()

    assert result.completed is True
    assert app_fixture.queue.flush_called is True
    flush = app_fixture.trace.index("flush_checkpoint")
    pause = app_fixture.trace.index("transition:paused")
    disconnect = app_fixture.trace.index("disconnect_smb")
    close_db = app_fixture.trace.index("close_db")
    assert flush < pause < disconnect < close_db


def test_shutdown_timeout_reports_safe_pausing_and_keeps_resources_open(app_fixture) -> None:
    app_fixture.service.start()

    result = app_fixture.service.request_shutdown(timeout=0.001)

    assert result.completed is False
    assert result.timed_out is True
    assert "安全暂停" in result.message
    assert app_fixture.smb.closed is False
    assert app_fixture.repository.closed is False


def test_shutdown_rejects_new_tasks(app_fixture) -> None:
    app_fixture.service.start()
    app_fixture.queue.boundary_reached.set()
    app_fixture.service.request_shutdown()

    with pytest.raises(RuntimeError, match="stopping"):
        app_fixture.service.enqueue("later")


def test_real_queue_timeout_keeps_resources_until_engine_reaches_boundary(app_fixture) -> None:
    from nasmove.core.states import TaskState
    from nasmove.transfer.transfer_engine import QueueCoordinator, TaskResult

    started = Event()
    release = Event()

    class BlockingEngine:
        def run_task(self, task_id, token):
            del task_id
            started.set()
            while not token.pause_requested:
                release.wait(0.001)
            release.wait()
            return TaskResult(False, TaskState.PAUSED)

    queue = QueueCoordinator(BlockingEngine())
    app_fixture.service = type(app_fixture.service)(
        repository=app_fixture.repository,
        queue=queue,
        smb_gateway=app_fixture.smb,
        credential_store=app_fixture.credentials,
        lock_path=app_fixture.lock_path,
    )
    app_fixture.service.start()
    worker = Thread(target=queue.run_next)
    worker.start()
    assert started.wait(1)

    first = app_fixture.service.request_shutdown(timeout=0.01)

    assert first.completed is False
    assert first.timed_out is True
    assert app_fixture.smb.closed is False
    assert app_fixture.repository.closed is False

    release.set()
    worker.join(1)
    second = app_fixture.service.request_shutdown(timeout=1)

    assert second.completed is True
    from nasmove.app import SingleInstanceLock

    probe = SingleInstanceLock(app_fixture.lock_path)
    assert probe.acquire() is True
    probe.release()


def test_default_repository_is_lazy_until_lock_is_owned(app_fixture, monkeypatch) -> None:
    import nasmove.app as app_module
    from nasmove.app import ApplicationService

    first_lock = ApplicationService(
        repository=app_fixture.repository,
        queue=app_fixture.queue,
        lock_path=app_fixture.lock_path,
    )
    assert first_lock.start().started is True

    created: list[object] = []

    class RepositoryFactory:
        def __init__(self, path):
            created.append(path)

    monkeypatch.setattr(app_module, "SqliteTaskRepository", RepositoryFactory)
    second = ApplicationService(
        queue=app_fixture.queue,
        lock_path=app_fixture.lock_path,
        database_path=app_fixture.lock_path.with_name("nasmove.db"),
    )

    report = second.start()

    assert report.already_running is True
    assert created == []
    first_lock.request_shutdown(timeout=1)


def test_flush_failure_returns_safe_result_and_releases_lock(app_fixture) -> None:
    app_fixture.service.start()
    app_fixture.queue.boundary_reached.set()
    app_fixture.queue.fail_flush = True

    result = app_fixture.service.request_shutdown(timeout=1)

    assert result.completed is False
    assert result.error is not None
    from nasmove.app import SingleInstanceLock

    probe = SingleInstanceLock(app_fixture.lock_path)
    assert probe.acquire() is True
    probe.release()


def test_pause_persistence_failure_returns_safe_result_and_releases_lock(app_fixture) -> None:
    app_fixture.service.start()
    app_fixture.queue.boundary_reached.set()
    app_fixture.repository.fail_transition = True

    result = app_fixture.service.request_shutdown(timeout=1)

    assert result.completed is False
    assert result.error is not None
    from nasmove.app import SingleInstanceLock

    probe = SingleInstanceLock(app_fixture.lock_path)
    assert probe.acquire() is True
    probe.release()


def test_shutdown_can_be_retried_after_timeout(app_fixture) -> None:
    app_fixture.service.start()
    first = app_fixture.service.request_shutdown(timeout=0.001)
    assert first.timed_out is True

    app_fixture.queue.boundary_reached.set()
    second = app_fixture.service.request_shutdown(timeout=1)

    assert second.completed is True
