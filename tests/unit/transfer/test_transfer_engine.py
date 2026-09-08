from dataclasses import replace

from nasmove.core.model import RemotePath
from nasmove.core.ports import SessionInfo
from nasmove.core.states import ItemState, SourceKind, TaskState, TransferAction
from nasmove.transfer.checkpoint_writer import CancellationToken, CheckpointWriter
from nasmove.transfer.commit import TargetCommitter
from nasmove.transfer.deletion import SourceDeletionService
from nasmove.transfer.recovery import RecoveryCoordinator
from nasmove.transfer.transfer_engine import TaskResult, TransferEngine
from nasmove.transfer.verification import IntegrityVerifier
from tests.fixtures.builders import build_task_record
from tests.fixtures.transfer import (
    FakeDependencies,
    TransferLocal,
    TransferRemote,
    TransferRepository,
    TransferToken,
)


def test_move_item_follows_copy_verify_commit_delete_order(engine_fixture) -> None:
    result = engine_fixture.engine.run_task(engine_fixture.move_task.id, engine_fixture.token)

    assert result.success is True
    assert engine_fixture.trace == [
        "transfer",
        "full_verify",
        "commit",
        "authorize_delete",
        "delete_source",
    ]


def test_copy_item_does_not_delete_source(engine_fixture) -> None:
    copy_task = replace(engine_fixture.move_task, action=TransferAction.COPY)
    engine_fixture.repository.tasks[copy_task.id] = copy_task
    result = engine_fixture.engine.run_task(copy_task.id, engine_fixture.token)

    assert result.success is True
    assert "authorize_delete" not in engine_fixture.trace
    assert "delete_source" not in engine_fixture.trace


def test_cancel_persists_safe_state_before_event(engine_fixture) -> None:
    engine_fixture.token.request_cancel()
    result = engine_fixture.engine.run_task(engine_fixture.move_task.id, engine_fixture.token)

    assert result.success is False
    assert result.state is TaskState.CANCELED
    assert engine_fixture.repository.get_task(engine_fixture.move_task.id).state is TaskState.CANCELED
    assert engine_fixture.events[-1].state is TaskState.CANCELED


def test_verification_failure_retains_source_and_fails_task(engine_fixture) -> None:
    engine_fixture.verifier.matches = False
    result = engine_fixture.engine.run_task(engine_fixture.move_task.id, engine_fixture.token)

    assert result.success is False
    assert result.state is TaskState.FAILED
    assert engine_fixture.repository.get_item(engine_fixture.item.id).state is ItemState.VERIFY_FAILED


def test_committer_owns_committed_transition_and_engine_continues_to_delete(engine_fixture) -> None:
    result = engine_fixture.engine.run_task(engine_fixture.move_task.id, engine_fixture.token)

    assert result.success is True
    assert engine_fixture.repository.get_item(engine_fixture.item.id).state is ItemState.COMMITTED
    assert engine_fixture.events[-1].state is TaskState.COMPLETED


def test_item_enumeration_failure_is_persisted_and_published(engine_fixture) -> None:
    def fail_list(_task_id):
        raise OSError("database unavailable")

    engine_fixture.repository.list_items = fail_list
    result = engine_fixture.engine.run_task(engine_fixture.move_task.id, engine_fixture.token)

    assert result.success is False
    assert result.state is TaskState.FAILED
    assert engine_fixture.repository.get_task(engine_fixture.move_task.id).state is TaskState.FAILED
    assert engine_fixture.events[-1].error is not None


def test_transient_network_failure_during_recovery_waits_for_network(engine_fixture) -> None:
    def fail_recovery(_item_id):
        raise ConnectionResetError("NAS is restarting")

    engine_fixture.engine._recovery.find_safe_offset = fail_recovery

    result = engine_fixture.engine.run_task(engine_fixture.move_task.id, engine_fixture.token)

    assert result.success is False
    assert result.state is TaskState.WAITING_FOR_NETWORK
    assert (
        engine_fixture.repository.get_task(engine_fixture.move_task.id).state
        is TaskState.WAITING_FOR_NETWORK
    )
    assert engine_fixture.repository.get_item(engine_fixture.item.id).state is ItemState.PLANNED


def test_task_result_is_immutable_and_has_safe_default_error() -> None:
    result = TaskResult(success=True, state=TaskState.COMPLETED)
    assert result.error is None


def test_move_empty_directory_runs_create_verify_commit_delete_protocol() -> None:
    trace: list[str] = []
    local = TransferLocal(b"", trace)
    remote = TransferRemote(trace)
    repository = TransferRepository(trace)
    support = FakeDependencies(local, remote, repository, trace, TransferToken())
    item = support.item(size=0)
    item = replace(
        item,
        source_fingerprint=replace(item.source_fingerprint, kind=SourceKind.EMPTY_DIRECTORY),
        final_path=RemotePath("target/empty"),
        temp_path=RemotePath("target/.empty.part"),
        state=ItemState.PLANNED,
    )
    local.fingerprint_override = item.source_fingerprint
    task = replace(
        build_task_record(),
        id=item.task_id,
        action=TransferAction.MOVE,
        state=TaskState.QUEUED,
        total_files=1,
        total_bytes=0,
    )
    repository.items[item.id] = item
    repository.tasks[task.id] = task
    session = SessionInfo("3.1.1", True, True, 1)
    verifier = IntegrityVerifier(repository, local, remote, session)
    engine = TransferEngine(
        repository,
        recovery=RecoveryCoordinator(repository, local, remote, session),
        checkpoint_writer=CheckpointWriter(repository, local, remote),
        verifier=verifier,
        committer=TargetCommitter(repository, remote, session),
        deletion_service=SourceDeletionService(repository, local, remote, verifier),
        session=session,
    )

    result = engine.run_task(task.id, CancellationToken())

    assert result.success is True
    assert repository.get_item(item.id).state is ItemState.DONE
    assert "target/empty" in remote.directories
    assert local.source_exists is False
