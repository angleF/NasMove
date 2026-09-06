from __future__ import annotations

import inspect
from dataclasses import dataclass
from threading import Event, Lock
from typing import Protocol, cast

from nasmove.core.model import TaskId, TaskRecord, TransferItemId, TransferItemRecord
from nasmove.core.ports import SessionInfo
from nasmove.core.retry import RetryPolicy
from nasmove.core.states import ItemState, TaskState, TransferAction
from nasmove.transfer.checkpoint_writer import CancellationToken, CopyOutcome
from nasmove.transfer.recovery import RecoveryDecision, RecoveryDisposition


class EventSink(Protocol):
    def publish(self, event: TransferEvent) -> None: ...


class Repository(Protocol):
    def get_task(self, task_id: TaskId) -> TaskRecord: ...

    def list_items(self, task_id: TaskId) -> list[TransferItemRecord]: ...

    def get_item(self, item_id: TransferItemId) -> TransferItemRecord: ...

    def transition_task(self, task_id: TaskId, expected: TaskState, target: TaskState) -> None: ...

    def transition_item(self, item_id: TransferItemId, expected: ItemState, target: ItemState) -> None: ...


class Recovery(Protocol):
    def find_safe_offset(self, item_id: TransferItemId) -> RecoveryDecision: ...


class CopyWriter(Protocol):
    def copy(self, item: TransferItemRecord, start_offset: int, session: SessionInfo, **kwargs: object) -> object: ...


class Verifier(Protocol):
    def verify_full(self, item: TransferItemRecord) -> object: ...


class Committer(Protocol):
    def commit(self, item: TransferItemRecord, verification: object) -> object: ...


class Deletion(Protocol):
    def delete_verified_source(self, item_id: TransferItemId, session: SessionInfo) -> object: ...


@dataclass(frozen=True, slots=True)
class TransferEvent:
    task_id: TaskId
    state: TaskState
    item_id: TransferItemId | None = None
    error: BaseException | None = None
    kind: str = "state"


@dataclass(frozen=True, slots=True)
class TaskResult:
    success: bool
    state: TaskState
    error: BaseException | None = None
    warnings: tuple[str, ...] = ()
    completed_items: int = 0

    @property
    def completed(self) -> bool:
        return self.success


def _requested(token: object, name: str) -> bool:
    value = getattr(token, name, False)
    if callable(value):
        value = value()
    return type(value) is bool and value


class TransferEngine:
    """Run one persisted task through the durable transfer protocol.

    The engine is deliberately synchronous.  The application owns the worker
    thread; keeping this coordinator single-threaded makes the ordering of
    checkpoint, verification, commit and deletion observable and resumable.
    """

    def __init__(
        self,
        repository: Repository,
        recovery: object | None = None,
        checkpoint_writer: object | None = None,
        verifier: object | None = None,
        committer: object | None = None,
        deletion_service: object | None = None,
        *,
        recovery_coordinator: object | None = None,
        integrity_verifier: object | None = None,
        target_committer: object | None = None,
        source_deletion: object | None = None,
        smb_gateway: object | None = None,
        session: SessionInfo | None = None,
        event_sink: EventSink | None = None,
        retry_policy: RetryPolicy | None = None,
        progress_tracker: object | None = None,
    ) -> None:
        self._repository = repository
        self._recovery = cast(Recovery | None, recovery if recovery is not None else recovery_coordinator)
        self._writer = cast(CopyWriter | None, checkpoint_writer)
        self._verifier = cast(Verifier | None, verifier if verifier is not None else integrity_verifier)
        self._committer = cast(Committer | None, committer if committer is not None else target_committer)
        self._deletion = cast(Deletion | None, deletion_service if deletion_service is not None else source_deletion)
        self._smb = smb_gateway
        self._session = session
        self._events = event_sink
        self._retry_policy = retry_policy or RetryPolicy()
        self._progress = progress_tracker
        missing = [
            name
            for name, value in (
                ("recovery", self._recovery),
                ("checkpoint_writer", self._writer),
                ("verifier", self._verifier),
                ("committer", self._committer),
            )
            if value is None
        ]
        if missing:
            raise TypeError("missing transfer services: " + ", ".join(missing))

    def run_task(self, task_id: TaskId, token: CancellationToken) -> TaskResult:
        task = self._repository.get_task(task_id)
        if _requested(token, "cancel_requested"):
            return self._finish(task, TaskState.CANCELED, False, kind="canceled")
        if _requested(token, "pause_requested"):
            return self._finish(task, TaskState.PAUSED, False, kind="paused")
        if task.state is TaskState.CANCELED:
            return TaskResult(False, TaskState.CANCELED)
        self._transition_task_if_needed(task, TaskState.RUNNING)
        try:
            items = list(self._repository.list_items(task_id))
        except Exception as error:  # noqa: BLE001 - persistence failure must be durable and visible
            return self._fail_task(task, error, None)
        completed = 0
        warnings: list[str] = []

        for completed, item in enumerate(items, 1):
            result = self._run_item(item, task, token)
            if isinstance(result, TaskResult):
                return result
            if result:
                warnings.append(result)
            if _requested(token, "cancel_requested"):
                return self._finish(task, TaskState.CANCELED, False, completed, kind="canceled")
            if _requested(token, "pause_requested"):
                return self._finish(task, TaskState.PAUSED, False, completed, kind="paused")

        if not items:
            completed = 0
        current_task = self._repository.get_task(task_id)
        if current_task.state is TaskState.RUNNING:
            self._transition_task_if_needed(current_task, TaskState.VERIFYING)
            current_task = self._repository.get_task(task_id)
        if current_task.state is TaskState.VERIFYING:
            self._transition_task_if_needed(current_task, TaskState.COMMITTING)
            current_task = self._repository.get_task(task_id)
        target_state = TaskState.COMPLETED_WITH_WARNINGS if warnings else TaskState.COMPLETED
        if current_task.state is TaskState.COMMITTING:
            self._transition_task_if_needed(current_task, target_state)
        self._publish(TransferEvent(task_id, target_state, kind="completed"))
        return TaskResult(True, target_state, warnings=tuple(warnings), completed_items=completed)

    def _run_item(
        self, original_item: TransferItemRecord, task: TaskRecord, token: CancellationToken
    ) -> str | TaskResult | None:
        item = self._repository.get_item(original_item.id)
        session = self._session_for(task)
        recovery = self._recovery
        verifier = self._verifier
        committer = self._committer
        if recovery is None or verifier is None or committer is None:
            raise RuntimeError("transfer services are unavailable")
        try:
            if item.state in {ItemState.COMMITTED, ItemState.SOURCE_DELETE_AUTHORIZED, ItemState.DONE}:
                return self._delete_if_needed(item, task, session)
            decision = recovery.find_safe_offset(item.id)
            if decision.disposition is RecoveryDisposition.SOURCE_CHANGED:
                self._safe_item_transition(item, ItemState.SOURCE_CHANGED)
                return self._fail_task(task, RuntimeError("source changed during recovery"), item)
            if decision.disposition is RecoveryDisposition.FINAL_CONFIRMED:
                return self._fail_task(task, RuntimeError("final target requires recovery confirmation"), item)
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
                copy_result = self._copy(item, decision.safe_offset, session, token)
                copied = getattr(copy_result, "bytes_copied", 0)
                if type(copied) is int:
                    copied = max(0, copied - decision.safe_offset)
                self._record_progress("record_copy", copied)
                outcome = getattr(copy_result, "outcome", CopyOutcome.COMPLETED)
                if outcome is CopyOutcome.CANCELLED or str(outcome) == CopyOutcome.CANCELLED.value:
                    self._safe_item_transition(item, ItemState.INTERRUPTED)
                    return self._finish(task, TaskState.CANCELED, False, kind="canceled")
                if outcome is CopyOutcome.PAUSED or str(outcome) == CopyOutcome.PAUSED.value:
                    self._safe_item_transition(item, ItemState.INTERRUPTED)
                    return self._finish(task, TaskState.PAUSED, False, kind="paused")
                if outcome is CopyOutcome.INTERRUPTED or str(outcome) == CopyOutcome.INTERRUPTED.value:
                    error = getattr(copy_result, "error", None) or OSError("transfer interrupted")
                    retryable = self._retry_policy.is_retryable(error)
                    self._safe_item_transition(item, ItemState.WAITING_RETRY if retryable else ItemState.INTERRUPTED)
                    state = TaskState.WAITING_FOR_NETWORK if retryable else TaskState.FAILED
                    return self._fail_task(task, error, item, state=state)
                self._safe_item_transition(item, ItemState.TRANSFERRED)
                item = self._repository.get_item(item.id)

            self._safe_item_transition(item, ItemState.VERIFYING)
            item = self._repository.get_item(item.id)
            verification = verifier.verify_full(item)
            self._record_progress("record_verification", getattr(verification, "remote_bytes", 0))
            if getattr(verification, "matches", False) is not True or getattr(
                verification, "source_unchanged", False
            ) is not True:
                self._safe_item_transition(item, ItemState.VERIFY_FAILED)
                return self._fail_task(task, RuntimeError("full verification failed"), item)
            self._safe_item_transition(item, ItemState.VERIFIED)
            item = self._repository.get_item(item.id)
            committer.commit(item, verification)
            item = self._repository.get_item(item.id)
            if item.state is not ItemState.COMMITTED:
                raise RuntimeError("target committer did not persist committed state")
            return self._delete_if_needed(item, task, session)
        except Exception as error:  # noqa: BLE001 - boundary failures need durable classification
            if item.state in {
                ItemState.TRANSFERRING,
                ItemState.TRANSFERRED,
                ItemState.VERIFYING,
                ItemState.VERIFIED,
            }:
                self._safe_item_transition(item, ItemState.INTERRUPTED)
            elif item.state is ItemState.SOURCE_DELETE_AUTHORIZED:
                self._safe_item_transition(item, ItemState.SOURCE_RETAINED)
            return self._fail_task(task, error, item)

    def _delete_if_needed(self, item: TransferItemRecord, task: TaskRecord, session: SessionInfo) -> str | None:
        if task.action is not TransferAction.MOVE:
            if item.state is ItemState.COMMITTED:
                self._safe_item_transition(item, ItemState.DONE)
            return None
        if self._deletion is None:
            raise RuntimeError("source deletion service is unavailable")
        deletion = self._deletion
        deletion_result = deletion.delete_verified_source(item.id, session)
        if getattr(deletion_result, "state", None) is ItemState.SOURCE_RETAINED:
            return "source retained because deletion failed"
        return None

    def _copy(self, item: TransferItemRecord, offset: int, session: SessionInfo, token: CancellationToken) -> object:
        writer = self._writer
        if writer is None:
            raise RuntimeError("checkpoint writer is unavailable")
        method = writer.copy
        if "token" in inspect.signature(method).parameters:
            return method(item, offset, session, token=token)
        return method(item, offset, session)

    def _session_for(self, task: TaskRecord) -> SessionInfo:
        if self._session is not None:
            return self._session
        session = getattr(self._recovery, "session", None)
        if isinstance(session, SessionInfo):
            return session
        if self._smb is not None:
            current = getattr(self._smb, "current_session", None)
            if isinstance(current, SessionInfo):
                return current
        return SessionInfo("unknown", False, False, 1)

    def _transition_task_if_needed(self, task: TaskRecord, target: TaskState) -> None:
        if task.state is target:
            return
        method = getattr(self._repository, "transition_task", None)
        if callable(method):
            method(task.id, task.state, target)

    def _safe_item_transition(self, item: TransferItemRecord, target: ItemState) -> None:
        if item.state is target or item.state is ItemState.DONE:
            return
        method = getattr(self._repository, "transition_item", None)
        if callable(method):
            method(item.id, item.state, target)

    def _fail_task(
        self,
        task: TaskRecord,
        error: BaseException,
        item: TransferItemRecord | None,
        *,
        state: TaskState = TaskState.FAILED,
    ) -> TaskResult:
        current = self._repository.get_task(task.id)
        if current.state is not state:
            self._transition_task_if_needed(current, state)
        self._publish(TransferEvent(task.id, state, None if item is None else item.id, error, "failed"))
        return TaskResult(False, state, error=error)

    def _finish(
        self,
        task: TaskRecord,
        state: TaskState,
        success: bool,
        completed: int = 0,
        *,
        kind: str,
    ) -> TaskResult:
        current = self._repository.get_task(task.id)
        if current.state is not state:
            self._transition_task_if_needed(current, state)
        self._publish(TransferEvent(task.id, state, kind=kind))
        return TaskResult(success, state, completed_items=completed)

    def _publish(self, event: TransferEvent) -> None:
        if self._events is not None:
            self._events.publish(event)

    def _record_progress(self, method_name: str, value: object) -> None:
        if self._progress is None or type(value) is not int or value <= 0:
            return
        method = getattr(self._progress, method_name, None)
        if callable(method):
            method(value)


class QueueCoordinator:
    """A stable, synchronous FIFO queue with one active engine invocation."""

    def __init__(self, engine: TransferEngine, repository: Repository | None = None) -> None:
        self._engine = engine
        self._repository = repository
        self._queue: list[TaskId] = []
        self._run_lock = Lock()
        self._lifecycle_lock = Lock()
        self._active_token: CancellationToken | None = None
        self._active_done = Event()
        self._active_done.set()
        self._accepting = True
        self._pending_pause = False

    def enqueue(self, task_id: TaskId | str) -> None:
        with self._lifecycle_lock:
            if not self._accepting:
                raise RuntimeError("queue is stopping")
            normalized = TaskId(str(task_id))
            if normalized not in self._queue:
                self._queue.append(normalized)

    def stop_accepting(self) -> None:
        with self._lifecycle_lock:
            self._accepting = False

    def request_pause(self) -> None:
        with self._lifecycle_lock:
            token = self._active_token
            if token is None:
                # No token is published yet, so latch the request onto the
                # next token published within this run.
                self._pending_pause = True
                return
        token.request_pause()

    def wait_for_safe_boundary(self, timeout: float) -> bool:
        """Wait until the current engine call returns after a safe block boundary."""
        return self._active_done.wait(timeout)

    def flush_and_checkpoint(self) -> None:
        """Formal lifecycle hook; the writer flushes before each durable checkpoint."""
        return

    def run_next(self, token: CancellationToken | None = None) -> TaskResult | None:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("queue coordinator is already running a task")
        active_token = token or CancellationToken()
        with self._lifecycle_lock:
            if not self._accepting:
                # A shutdown completed inside the publication window: never
                # start a task that would not observe the pause.
                self._run_lock.release()
                return None
            self._active_token = active_token
            self._active_done.clear()
            if self._pending_pause:
                active_token.request_pause()
                self._pending_pause = False
        try:
            with self._lifecycle_lock:
                task_id: TaskId | None = self._queue.pop(0) if self._queue else None
            if task_id is None and self._repository is not None:
                next_task = getattr(self._repository, "next_queued_task", None)
                if not callable(next_task):
                    raise TypeError("queue repository must provide next_queued_task()")
                task = next_task()
                task_id = None if task is None else task.id
            if task_id is None:
                return None
            return self._engine.run_task(task_id, active_token)
        finally:
            with self._lifecycle_lock:
                self._active_token = None
                self._active_done.set()
            self._run_lock.release()


__all__ = ["QueueCoordinator", "TaskResult", "TransferEngine", "TransferEvent"]
