from dataclasses import replace

from nasmove.core.states import ItemState, TaskState, TransferAction
from nasmove.transfer.transfer_engine import TaskResult


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


def test_task_result_is_immutable_and_has_safe_default_error() -> None:
    result = TaskResult(success=True, state=TaskState.COMPLETED)
    assert result.error is None
