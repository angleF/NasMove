import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from nasmove.core.states import ItemState, TaskState, TransferAction
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from nasmove.transfer.checkpoint_writer import (
    CancellationToken,
    CheckpointWriter,
    CopyOutcome,
    CopyResult,
)
from nasmove.transfer.commit import TargetCommitter
from nasmove.transfer.deletion import SourceDeletionService
from nasmove.transfer.item_worker import ItemRunOutcome, TransferItemWorker
from nasmove.transfer.recovery import RecoveryCoordinator, RecoveryDecision, RecoveryDisposition
from nasmove.transfer.transfer_engine import (
    _StopController,
    _StopReason,
    _WorkerCancellationToken,
)
from nasmove.transfer.verification import IntegrityVerifier


def test_item_worker_releases_resources_after_success_and_close_is_idempotent(item_worker_fixture) -> None:
    fixture = item_worker_fixture

    outcome = fixture.worker.run(fixture.item, fixture.task, CancellationToken())
    fixture.worker.close()

    assert outcome == ItemRunOutcome(fixture.item.id, ItemState.DONE)
    assert fixture.remote.disconnect_calls == 1
    assert fixture.repository.release_calls == 1
    assert fixture.local.source_exists is False
    assert fixture.local.trash_calls == [fixture.item.source_path]


def test_item_worker_never_transitions_task_state_and_events_identify_item(item_worker_fixture) -> None:
    fixture = item_worker_fixture

    def forbidden_task_transition(*args) -> None:
        pytest.fail("item workers must not transition task state")

    fixture.repository.transition_task = forbidden_task_transition
    fixture.worker.run(fixture.item, fixture.task, CancellationToken())

    assert fixture.repository.get_task(fixture.task.id).state is TaskState.RUNNING
    assert fixture.events
    assert all(event.task_id == fixture.task.id and event.item_id == fixture.item.id for event in fixture.events)


def test_item_worker_failed_full_verification_cannot_move_source_to_trash(item_worker_fixture) -> None:
    fixture = item_worker_fixture
    fixture.remote.replace_after_read = True

    outcome = fixture.worker.run(fixture.item, fixture.task, CancellationToken())

    assert outcome.error is not None
    assert outcome.state is ItemState.VERIFY_FAILED
    assert fixture.repository.get_item(fixture.item.id).state is ItemState.VERIFY_FAILED
    assert fixture.local.trash_calls == []
    assert fixture.local.source_exists is True
    assert fixture.remote.disconnect_calls == fixture.repository.release_calls == 1


@pytest.mark.parametrize(
    "boundary",
    ["before-verification", "after-verification", "before-commit", "before-source-delete"],
)
@pytest.mark.parametrize(
    "stop_source",
    ["user-cancel", "user-pause", "internal-stop", "internal-stop-then-user-cancel"],
)
def test_item_worker_stop_at_late_lifecycle_boundary_never_commits_or_trashes_after_stop(
    item_worker_fixture, boundary, stop_source
) -> None:
    fixture = item_worker_fixture
    user_token = CancellationToken()
    stop_controller = _StopController()
    token = (
        _WorkerCancellationToken(user_token, stop_controller)
        if stop_source.startswith("internal-stop")
        else user_token
    )
    if stop_source == "user-cancel":
        trigger_stop = user_token.request_cancel
    elif stop_source == "user-pause":
        trigger_stop = user_token.request_pause
    else:
        def request_internal_stop() -> None:
            stop_controller.freeze(
                _StopReason("error", RuntimeError("peer failed"))
            )
            if stop_source == "internal-stop-then-user-cancel":
                user_token.request_cancel()

        trigger_stop = request_internal_stop

    verification_calls = 0
    commit_calls = 0
    original_copy = fixture.worker._writer.copy
    original_verify = fixture.worker._verifier.verify_full
    original_commit = fixture.worker._committer.commit
    original_publish = fixture.worker._events.publish

    def copy(*args, **kwargs):
        result = original_copy(*args, **kwargs)
        if boundary == "before-verification":
            trigger_stop()
        return result

    def verify(*args, **kwargs):
        nonlocal verification_calls
        verification_calls += 1
        result = original_verify(*args, **kwargs)
        if boundary == "after-verification":
            trigger_stop()
        return result

    def commit(*args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        result = original_commit(*args, **kwargs)
        if boundary == "before-source-delete":
            trigger_stop()
        return result

    def publish(event) -> None:
        original_publish(event)
        if boundary == "before-commit" and event.state is TaskState.COMMITTING:
            trigger_stop()

    fixture.worker._writer.copy = copy
    fixture.worker._verifier.verify_full = verify
    fixture.worker._committer.commit = commit
    fixture.worker._events.publish = publish

    outcome = fixture.worker.run(fixture.item, fixture.task, token)

    assert outcome.error is None
    assert outcome.state is ItemState.INTERRUPTED
    assert fixture.local.trash_calls == []
    assert fixture.local.source_exists is True
    assert commit_calls == (1 if boundary == "before-source-delete" else 0)
    assert verification_calls == (0 if boundary == "before-verification" else 1)
    if stop_source.startswith("internal-stop"):
        assert not any(
            event.state in {TaskState.CANCELED, TaskState.PAUSED}
            for event in fixture.events
        )
        assert any(
            event.state is TaskState.RUNNING and event.kind == "interrupted"
            for event in fixture.events
        )
        assert user_token.cancel_requested is (
            stop_source == "internal-stop-then-user-cancel"
        )
        assert user_token.pause_requested is False
    else:
        expected_state = (
            TaskState.CANCELED if stop_source == "user-cancel" else TaskState.PAUSED
        )
        assert any(event.state is expected_state for event in fixture.events)


@pytest.mark.parametrize("request_method", ["request_cancel", "request_pause"])
def test_item_worker_interrupted_copy_persists_safe_state_and_releases_resources(item_worker_fixture, request_method) -> None:
    fixture = item_worker_fixture
    token = CancellationToken()
    getattr(token, request_method)()

    outcome = fixture.worker.run(fixture.item, fixture.task, token)

    assert outcome.state is ItemState.INTERRUPTED
    assert outcome.error is None
    assert fixture.local.trash_calls == []
    assert fixture.remote.disconnect_calls == fixture.repository.release_calls == 1


@pytest.mark.parametrize("error,retryable,state", [
    (ConnectionResetError("NAS restarted"), True, ItemState.WAITING_RETRY),
    (PermissionError("access denied"), False, ItemState.INTERRUPTED),
])
def test_item_worker_classifies_copy_errors_and_retains_source(item_worker_fixture, error, retryable, state) -> None:
    fixture = item_worker_fixture

    def fail_copy(*args, **kwargs):
        return CopyResult(CopyOutcome.INTERRUPTED, 0, 0, error=error)

    fixture.worker._writer.copy = fail_copy

    outcome = fixture.worker.run(fixture.item, fixture.task, CancellationToken())

    assert outcome.error is error
    assert outcome.retryable is retryable
    assert outcome.state is state
    assert fixture.repository.get_item(fixture.item.id).state is state
    assert fixture.local.trash_calls == []
    assert fixture.remote.disconnect_calls == fixture.repository.release_calls == 1


def test_item_worker_source_change_is_persisted_and_returned(item_worker_fixture) -> None:
    fixture = item_worker_fixture
    fixture.worker._recovery.find_safe_offset = lambda _: RecoveryDecision(
        0, False, RecoveryDisposition.SOURCE_CHANGED
    )

    outcome = fixture.worker.run(fixture.item, fixture.task, CancellationToken())

    assert outcome.state is ItemState.SOURCE_CHANGED
    assert outcome.error is not None
    assert fixture.local.trash_calls == []


def test_item_worker_copy_keeps_source(item_worker_fixture) -> None:
    fixture = item_worker_fixture
    task = replace(fixture.task, action=TransferAction.COPY)
    fixture.repository.tasks[task.id] = task

    outcome = fixture.worker.run(fixture.item, task, CancellationToken())

    assert outcome.state is ItemState.DONE
    assert outcome.error is None
    assert fixture.local.source_exists is True
    assert fixture.local.trash_calls == []


def test_item_worker_cleanup_still_releases_repository_if_disconnect_raises(item_worker_fixture) -> None:
    fixture = item_worker_fixture

    def fail_disconnect() -> None:
        fixture.remote.disconnect_calls += 1
        raise OSError("disconnect failed")

    fixture.remote.disconnect = fail_disconnect
    with pytest.raises(OSError, match="disconnect failed"):
        fixture.worker.close()
    fixture.worker.close()

    assert fixture.remote.disconnect_calls == fixture.repository.release_calls == 1


def test_item_worker_borrowed_resources_are_not_closed(item_worker_fixture) -> None:
    fixture = item_worker_fixture
    worker = TransferItemWorker(
        fixture.repository,
        fixture.worker._recovery,
        fixture.worker._writer,
        fixture.worker._verifier,
        fixture.worker._committer,
        fixture.worker._deletion,
        smb_gateway=fixture.remote,
        session=fixture.worker._session,
        owns_resources=False,
    )

    outcome = worker.run(fixture.item, fixture.task, CancellationToken())
    worker.close()

    assert outcome.error is None
    assert fixture.remote.disconnect_calls == fixture.repository.release_calls == 0


def test_item_worker_session_initialization_failure_releases_resources(item_worker_fixture) -> None:
    fixture = item_worker_fixture
    failure = ConnectionResetError("session initialization failed")

    def fail_session():
        raise failure

    fixture.worker._session_for = fail_session

    outcome = fixture.worker.run(fixture.item, fixture.task, CancellationToken())
    fixture.worker.close()

    assert outcome.error is failure
    assert outcome.retryable is True
    assert outcome.state is ItemState.PLANNED
    assert fixture.repository.get_task(fixture.task.id).state is TaskState.RUNNING
    assert fixture.local.trash_calls == []
    assert fixture.remote.disconnect_calls == fixture.repository.release_calls == 1


def test_item_worker_repository_initialization_failure_still_releases_resources(item_worker_fixture) -> None:
    fixture = item_worker_fixture

    def fail_get_item(_item_id):
        raise sqlite3.OperationalError("database unavailable")

    fixture.repository.get_item = fail_get_item

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        fixture.worker.run(fixture.item, fixture.task, CancellationToken())
    fixture.worker.close()

    assert fixture.local.trash_calls == []
    assert fixture.remote.disconnect_calls == fixture.repository.release_calls == 1


@pytest.mark.parametrize("operation_fails", [False, True])
def test_item_worker_reports_cleanup_failure_without_losing_operation_error(
    item_worker_fixture, operation_fails
) -> None:
    fixture = item_worker_fixture
    operation_error = PermissionError("access denied")
    cleanup_error = OSError("disconnect failed")

    def fail_copy(*args, **kwargs):
        return CopyResult(CopyOutcome.INTERRUPTED, 0, 0, error=operation_error)

    def fail_disconnect() -> None:
        fixture.remote.disconnect_calls += 1
        raise cleanup_error

    if operation_fails:
        fixture.worker._writer.copy = fail_copy
    fixture.remote.disconnect = fail_disconnect

    outcome = fixture.worker.run(fixture.item, fixture.task, CancellationToken())
    fixture.worker.close()

    assert outcome.error is (operation_error if operation_fails else cleanup_error)
    assert outcome.state is (ItemState.INTERRUPTED if operation_fails else ItemState.DONE)
    assert outcome.retryable is False
    if operation_fails:
        assert "item worker cleanup failed: OSError" in operation_error.__notes__
        assert fixture.local.trash_calls == []
    assert fixture.remote.disconnect_calls == fixture.repository.release_calls == 1


@pytest.mark.parametrize("error,retryable,state", [
    (ConnectionResetError("NAS restarted"), True, ItemState.WAITING_RETRY),
    (PermissionError("access denied"), False, ItemState.INTERRUPTED),
])
def test_item_worker_verification_exception_retains_source_and_closes_resources(
    item_worker_fixture, error, retryable, state
) -> None:
    fixture = item_worker_fixture

    def fail_verification(_item):
        raise error

    fixture.worker._verifier.verify_full = fail_verification

    outcome = fixture.worker.run(fixture.item, fixture.task, CancellationToken())

    assert outcome.error is error
    assert outcome.retryable is retryable
    assert outcome.state is state
    assert fixture.repository.get_item(fixture.item.id).state is state
    assert fixture.local.trash_calls == []
    assert fixture.remote.disconnect_calls == fixture.repository.release_calls == 1


def test_item_worker_releases_only_its_sqlite_thread_connection(item_worker_fixture, tmp_path) -> None:
    fixture = item_worker_fixture
    repository = SqliteTaskRepository(tmp_path / "worker.db")
    repository.create_task(replace(fixture.task, state=TaskState.QUEUED), [fixture.item])
    repository.transition_task(fixture.task.id, TaskState.QUEUED, TaskState.RUNNING)
    main_connection = repository._connection
    session = fixture.worker._session

    def run_item():
        verifier = IntegrityVerifier(repository, fixture.local, fixture.remote, session)
        worker = TransferItemWorker(
            repository,
            RecoveryCoordinator(repository, fixture.local, fixture.remote, session),
            CheckpointWriter(repository, fixture.local, fixture.remote),
            verifier,
            TargetCommitter(repository, fixture.remote, session),
            SourceDeletionService(repository, fixture.local, fixture.remote, verifier),
            smb_gateway=fixture.remote,
            session=session,
        )
        worker_connection = repository._connection
        assert worker_connection is not main_connection
        outcome = worker.run(fixture.item, fixture.task, CancellationToken())
        worker.close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            worker_connection.execute("SELECT 1")
        assert repository._thread_connections.connection is None
        return outcome

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            outcome = pool.submit(run_item).result(timeout=5)

        assert outcome == ItemRunOutcome(fixture.item.id, ItemState.DONE)
        assert repository.get_item(fixture.item.id).state is ItemState.DONE
        assert repository.get_task(fixture.task.id).state is TaskState.RUNNING
        assert repository._connection is main_connection
        assert main_connection.execute("SELECT 1").fetchone()[0] == 1
        assert fixture.remote.disconnect_calls == 1
    finally:
        repository.close()


def test_item_worker_contains_a_missing_source_at_the_item_boundary(item_worker_fixture) -> None:
    # Pins the defect-C escape point: the item worker's ``except Exception``
    # boundary must deliver a missing local source as an ItemRunOutcome, never
    # as an exception escaping to the queue layer.
    fixture = item_worker_fixture
    fixture.local.source_exists = False

    outcome = fixture.worker.run(fixture.item, fixture.task, CancellationToken())

    assert outcome.error is not None
    assert outcome.state is not ItemState.DONE
    assert fixture.local.trash_calls == []
    assert fixture.repository.get_item(fixture.item.id).state is not ItemState.DONE
    assert fixture.remote.disconnect_calls == 1
