from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from nasmove.core.model import TaskId, TaskRecord, TransferItemId, TransferItemRecord
from nasmove.core.ports import SessionInfo
from nasmove.core.retry import RetryPolicy
from nasmove.core.states import ConflictPolicy, ItemState, TaskState, TransferAction
from nasmove.transfer.checkpoint_writer import CancellationToken, CopyOutcome
from nasmove.transfer.recovery import RecoveryDecision, RecoveryDisposition


@dataclass(frozen=True, slots=True)
class TransferEvent:
    task_id: TaskId
    state: TaskState
    item_id: TransferItemId | None = None
    error: BaseException | None = None
    kind: str = "state"
    retry_attempt: int | None = None
    retry_delay: float | None = None


class EventSink(Protocol):
    def publish(self, event: TransferEvent) -> None: ...


class ItemRepository(Protocol):
    def get_item(self, item_id: TransferItemId) -> TransferItemRecord: ...

    def transition_item(self, item_id: TransferItemId, expected: ItemState, target: ItemState) -> None: ...

    def release_thread_connection(self) -> None: ...


class Recovery(Protocol):
    def find_safe_offset(self, item_id: TransferItemId) -> RecoveryDecision: ...


class CopyWriter(Protocol):
    def copy(self, item: TransferItemRecord, start_offset: int, session: SessionInfo, **kwargs: object) -> object: ...


class Verifier(Protocol):
    def verify_full(
        self,
        item: TransferItemRecord,
        token: CancellationToken | None = None,
    ) -> object: ...


class Committer(Protocol):
    def commit(
        self,
        item: TransferItemRecord,
        verification: object,
        *,
        conflict_policy: ConflictPolicy,
    ) -> object: ...


class Deletion(Protocol):
    def delete_verified_source(self, item_id: TransferItemId, session: SessionInfo) -> object: ...


@dataclass(frozen=True, slots=True)
class ItemRunOutcome:
    item_id: TransferItemId
    state: ItemState
    warning: str | None = None
    error: BaseException | None = None
    retryable: bool = False


class ItemWorker(Protocol):
    def run(
        self, item: TransferItemRecord, task: TaskRecord, token: CancellationToken
    ) -> ItemRunOutcome: ...

    def close(self) -> None: ...


ItemWorkerFactory = Callable[[TaskRecord, str], ItemWorker]


def _requested(token: object, name: str) -> bool:
    value = getattr(token, name, False)
    if callable(value):
        value = value()
    return type(value) is bool and value


class TransferItemWorker:
    """Run one item's durable lifecycle using resources exclusive to its thread.

    The legacy serial engine borrows its retry runner's resources by passing
    ``owns_resources=False``. Factory-created workers own and close theirs.
    """

    def __init__(
        self,
        repository: ItemRepository,
        recovery: Recovery,
        checkpoint_writer: CopyWriter,
        verifier: Verifier,
        committer: Committer,
        deletion_service: Deletion | None = None,
        *,
        smb_gateway: object | None = None,
        session: SessionInfo | None = None,
        event_sink: EventSink | None = None,
        retry_policy: RetryPolicy | None = None,
        progress_tracker: object | None = None,
        owns_resources: bool = True,
    ) -> None:
        self._repository = repository
        self._recovery = recovery
        self._writer = checkpoint_writer
        self._verifier = verifier
        self._committer = committer
        self._deletion = deletion_service
        self._gateway = smb_gateway
        self._session = session
        self._events = event_sink
        self._retry_policy = retry_policy or RetryPolicy()
        self._progress = progress_tracker
        self._owns_resources = owns_resources
        self._closed = False

    def run(
        self, item: TransferItemRecord, task: TaskRecord, token: CancellationToken
    ) -> ItemRunOutcome:
        if self._closed:
            raise RuntimeError("item worker is closed")
        outcome: ItemRunOutcome | None = None
        try:
            outcome = self._run_lifecycle(item, task, token)
        finally:
            try:
                self.close()
            except Exception as error:
                if outcome is None:
                    raise
                if outcome.error is None:
                    outcome = replace(outcome, error=error, retryable=False)
                else:
                    outcome.error.add_note(f"item worker cleanup failed: {type(error).__name__}")
        return outcome

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._owns_resources:
            return
        try:
            disconnect = getattr(self._gateway, "disconnect", None)
            if callable(disconnect):
                disconnect()
        finally:
            self._repository.release_thread_connection()

    def _run_lifecycle(
        self, original_item: TransferItemRecord, task: TaskRecord, token: CancellationToken
    ) -> ItemRunOutcome:
        item = original_item
        try:
            item = self._repository.get_item(item.id)
            if item.state is ItemState.SKIPPED:
                return self._outcome(item, warning="条目已按冲突策略跳过，源文件保持不变")
            session = self._session_for()
            if item.state in {ItemState.COMMITTED, ItemState.SOURCE_DELETE_AUTHORIZED, ItemState.DONE}:
                return self._delete_if_needed(item, task, session, token)
            decision = self._recovery.find_safe_offset(item.id)
            if decision.disposition is RecoveryDisposition.SOURCE_CHANGED:
                self._safe_item_transition(item, ItemState.SOURCE_CHANGED)
                return self._failure(item, task, RuntimeError("source changed during recovery"))
            if decision.disposition is RecoveryDisposition.FINAL_CONFIRMED:
                return self._failure(item, task, RuntimeError("final target requires recovery confirmation"))
            if decision.disposition in {
                RecoveryDisposition.FINAL_UNSAFE,
                RecoveryDisposition.START_OVER,
                RecoveryDisposition.RESUME,
            }:
                if item.state in {
                    ItemState.PLANNED,
                    ItemState.INTERRUPTED,
                    ItemState.WAITING_RETRY,
                    ItemState.VERIFY_FAILED,
                }:
                    self._safe_item_transition(item, ItemState.TRANSFERRING)
                    item = self._repository.get_item(item.id)
                self._publish(TransferEvent(task.id, TaskState.RUNNING, item.id))
                copy_result = self._copy(item, decision.safe_offset, session, token)
                copied = getattr(copy_result, "bytes_copied", 0)
                if type(copied) is int:
                    copied = max(0, copied - decision.safe_offset)
                self._record_progress("record_copy", copied)
                copy_outcome = getattr(copy_result, "outcome", CopyOutcome.COMPLETED)
                if copy_outcome is CopyOutcome.CANCELLED or str(copy_outcome) == CopyOutcome.CANCELLED.value:
                    token.request_cancel()
                    interrupted = self._interrupt_if_requested(item, task, token)
                    if interrupted is None:
                        raise RuntimeError("copy canceled without a stop request")
                    return interrupted
                if copy_outcome is CopyOutcome.PAUSED or str(copy_outcome) == CopyOutcome.PAUSED.value:
                    token.request_pause()
                    interrupted = self._interrupt_if_requested(item, task, token)
                    if interrupted is None:
                        raise RuntimeError("copy paused without a stop request")
                    return interrupted
                if copy_outcome is CopyOutcome.INTERRUPTED or str(copy_outcome) == CopyOutcome.INTERRUPTED.value:
                    error = getattr(copy_result, "error", None) or OSError("transfer interrupted")
                    return self._failure(item, task, error)
                self._safe_item_transition(item, ItemState.TRANSFERRED)
                item = self._repository.get_item(item.id)

            interrupted = self._interrupt_if_requested(item, task, token)
            if interrupted is not None:
                return interrupted
            self._safe_item_transition(item, ItemState.VERIFYING)
            item = self._repository.get_item(item.id)
            self._publish(TransferEvent(task.id, TaskState.VERIFYING, item.id))
            verification = self._verify(item, token)
            interrupted = self._interrupt_if_requested(item, task, token)
            if interrupted is not None:
                return interrupted
            self._record_progress("record_verification", getattr(verification, "remote_bytes", 0))
            if getattr(verification, "matches", False) is not True or getattr(
                verification, "source_unchanged", False
            ) is not True:
                self._safe_item_transition(item, ItemState.VERIFY_FAILED)
                return self._failure(item, task, RuntimeError("full verification failed"))
            self._safe_item_transition(item, ItemState.VERIFIED)
            item = self._repository.get_item(item.id)
            interrupted = self._interrupt_if_requested(item, task, token)
            if interrupted is not None:
                return interrupted
            self._publish(TransferEvent(task.id, TaskState.COMMITTING, item.id))
            interrupted = self._interrupt_if_requested(item, task, token)
            if interrupted is not None:
                return interrupted
            commit_result = self._committer.commit(
                item, verification, conflict_policy=task.conflict_policy
            )
            item = self._repository.get_item(item.id)
            if getattr(commit_result, "committed", True) is False and item.state is ItemState.SKIPPED:
                return self._outcome(
                    item, warning="目标在提交前发生冲突，已按策略跳过；源文件保持不变"
                )
            if item.state is not ItemState.COMMITTED:
                raise RuntimeError("target committer did not persist committed state")
            return self._delete_if_needed(item, task, session, token)
        except InterruptedError as error:
            interrupted = self._interrupt_if_requested(item, task, token)
            if interrupted is not None:
                return interrupted
            return self._failure(item, task, error)
        except Exception as error:  # noqa: BLE001 - boundary failures need durable classification
            return self._failure(item, task, error)

    def _failure(self, item: TransferItemRecord, task: TaskRecord, error: BaseException) -> ItemRunOutcome:
        # Services can persist newer states before raising. Read the durable
        # state so an exception after commit cannot roll a committed item back.
        item = self._repository.get_item(item.id)
        retryable = self._retry_policy.is_retryable(error) and item.state not in {
            ItemState.COMMITTED, ItemState.SOURCE_DELETE_AUTHORIZED, ItemState.DONE,
        }
        if item.state in {
            ItemState.TRANSFERRING, ItemState.TRANSFERRED, ItemState.VERIFYING, ItemState.VERIFIED,
        }:
            retry_state = (
                ItemState.WAITING_RETRY
                if retryable and item.state in {ItemState.TRANSFERRING, ItemState.VERIFYING}
                else ItemState.INTERRUPTED
            )
            self._safe_item_transition(item, retry_state)
        elif item.state is ItemState.SOURCE_DELETE_AUTHORIZED:
            self._safe_item_transition(item, ItemState.SOURCE_RETAINED)
        state = TaskState.WAITING_FOR_NETWORK if retryable else TaskState.RUNNING
        self._publish(TransferEvent(task.id, state, item.id, error, "failed"))
        return self._outcome(item, error=error, retryable=retryable)

    def _outcome(
        self, item: TransferItemRecord, *, warning: str | None = None,
        error: BaseException | None = None, retryable: bool = False,
    ) -> ItemRunOutcome:
        current = self._repository.get_item(item.id)
        return ItemRunOutcome(current.id, current.state, warning, error, retryable)

    def _delete_if_needed(
        self,
        item: TransferItemRecord,
        task: TaskRecord,
        session: SessionInfo,
        token: CancellationToken,
    ) -> ItemRunOutcome:
        interrupted = self._interrupt_if_requested(item, task, token)
        if interrupted is not None:
            return interrupted
        if task.action is not TransferAction.MOVE:
            if item.state is ItemState.COMMITTED:
                self._safe_item_transition(item, ItemState.DONE)
            return self._outcome(item)
        if self._deletion is None:
            raise RuntimeError("source deletion service is unavailable")
        self._publish(TransferEvent(task.id, TaskState.DELETING_SOURCE, item.id))
        interrupted = self._interrupt_if_requested(item, task, token)
        if interrupted is not None:
            return interrupted
        result = self._deletion.delete_verified_source(item.id, session)
        warning = (
            "source retained because deletion failed"
            if getattr(result, "state", None) is ItemState.SOURCE_RETAINED else None
        )
        return self._outcome(item, warning=warning)

    def _interrupt_if_requested(
        self,
        item: TransferItemRecord,
        task: TaskRecord,
        token: CancellationToken,
    ) -> ItemRunOutcome | None:
        cancel_requested = _requested(token, "cancel_requested")
        pause_requested = _requested(token, "pause_requested")
        if not cancel_requested and not pause_requested:
            return None

        current = self._repository.get_item(item.id)
        if current.state in {
            ItemState.TRANSFERRING,
            ItemState.TRANSFERRED,
            ItemState.VERIFYING,
            ItemState.VERIFIED,
            ItemState.WAITING_RETRY,
        }:
            self._safe_item_transition(current, ItemState.INTERRUPTED)

        internal_stop = _requested(token, "internal_stop_requested")
        if internal_stop:
            state = TaskState.RUNNING
            kind = "interrupted"
        elif cancel_requested:
            state = TaskState.CANCELED
            kind = "canceled"
        else:
            state = TaskState.PAUSED
            kind = "paused"
        self._publish(TransferEvent(task.id, state, item.id, kind=kind))
        return ItemRunOutcome(item.id, ItemState.INTERRUPTED)

    def _copy(self, item: TransferItemRecord, offset: int, session: SessionInfo, token: CancellationToken) -> object:
        method = self._writer.copy
        if "token" in inspect.signature(method).parameters:
            return method(item, offset, session, token=token)
        return method(item, offset, session)

    def _verify(self, item: TransferItemRecord, token: CancellationToken) -> object:
        method = self._verifier.verify_full
        if "token" in inspect.signature(method).parameters:
            return method(item, token=token)
        return method(item)

    def _session_for(self) -> SessionInfo:
        if self._session is not None:
            return self._session
        session = getattr(self._recovery, "session", None)
        if isinstance(session, SessionInfo):
            return session
        current = getattr(self._gateway, "current_session", None)
        if isinstance(current, SessionInfo):
            return current
        return SessionInfo("unknown", False, False, 1)

    def _safe_item_transition(self, item: TransferItemRecord, target: ItemState) -> None:
        if item.state is not target and item.state is not ItemState.DONE:
            self._repository.transition_item(item.id, item.state, target)

    def _publish(self, event: TransferEvent) -> None:
        if self._events is not None:
            self._events.publish(event)

    def _record_progress(self, method_name: str, value: object) -> None:
        if self._progress is None or type(value) is not int or value <= 0:
            return
        method = getattr(self._progress, method_name, None)
        if callable(method):
            method(value)


__all__ = ["ItemRunOutcome", "ItemWorker", "ItemWorkerFactory", "TransferItemWorker"]
