from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from threading import Event, Lock
from time import monotonic
from typing import Protocol, cast

from nasmove.core.model import TaskId, TaskRecord, TransferItemId, TransferItemRecord
from nasmove.core.ports import SessionInfo
from nasmove.core.retry import RetryPolicy
from nasmove.core.states import ItemState, SourceKind, TaskState
from nasmove.transfer.checkpoint_writer import CancellationToken
from nasmove.transfer.item_worker import (
    Committer,
    CopyWriter,
    Deletion,
    EventSink,
    ItemRunOutcome,
    ItemWorkerFactory,
    Recovery,
    TransferEvent,
    TransferItemWorker,
    Verifier,
)


class Repository(Protocol):
    def get_task(self, task_id: TaskId) -> TaskRecord: ...

    def list_items(self, task_id: TaskId) -> list[TransferItemRecord]: ...

    def get_item(self, item_id: TransferItemId) -> TransferItemRecord: ...

    def transition_task(self, task_id: TaskId, expected: TaskState, target: TaskState) -> None: ...

    def transition_item(self, item_id: TransferItemId, expected: ItemState, target: ItemState) -> None: ...

    def release_thread_connection(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TaskResult:
    success: bool
    state: TaskState
    error: BaseException | None = None
    warnings: tuple[str, ...] = ()
    completed_items: int = 0
    failed_items: int = 0
    task_id: TaskId | None = None

    @property
    def completed(self) -> bool:
        return self.success


def _requested(token: object, name: str) -> bool:
    value = getattr(token, name, False)
    if callable(value):
        value = value()
    return type(value) is bool and value


def _request(token: object, name: str) -> None:
    method = getattr(token, name, None)
    if callable(method):
        method()


class _WorkerCancellationToken:
    """Combine user intent with an engine-private stop signal.

    Item workers may call ``request_cancel`` after observing a canceled copy.
    Keeping that mutation on the private event prevents one failed item from
    turning the caller's token, and therefore the task, into user-canceled.
    """

    def __init__(
        self,
        user_token: CancellationToken,
        stop_signal: Event | _StopController,
    ) -> None:
        self._user_token = user_token
        self._stop_controller = (
            stop_signal if isinstance(stop_signal, _StopController) else None
        )
        self._stop_event = stop_signal if isinstance(stop_signal, Event) else None

    @property
    def pause_requested(self) -> bool:
        reason = None if self._stop_controller is None else self._stop_controller.reason
        return _requested(self._user_token, "pause_requested") or (
            reason is not None and reason.kind == "pause"
        )

    def request_pause(self) -> None:
        if self._stop_controller is not None:
            kind = "pause" if _requested(self._user_token, "pause_requested") else "error"
            error = None if kind == "pause" else RuntimeError(
                "worker requested pause without a user pause"
            )
            self._stop_controller.freeze(_StopReason(kind, error))
        elif self._stop_event is not None:
            self._stop_event.set()

    @property
    def cancel_requested(self) -> bool:
        reason = None if self._stop_controller is None else self._stop_controller.reason
        controller_cancel = reason is not None and reason.kind in {"cancel", "error"}
        event_cancel = self._stop_event is not None and self._stop_event.is_set()
        return (
            _requested(self._user_token, "cancel_requested")
            or controller_cancel
            or event_cancel
        )

    def request_cancel(self) -> None:
        if self._stop_controller is not None:
            kind = "cancel" if _requested(self._user_token, "cancel_requested") else "error"
            error = None if kind == "cancel" else RuntimeError(
                "worker requested cancellation without a user cancellation"
            )
            self._stop_controller.freeze(_StopReason(kind, error))
        elif self._stop_event is not None:
            self._stop_event.set()

    @property
    def internal_stop_requested(self) -> bool:
        if self._stop_controller is not None:
            reason = self._stop_controller.reason
            return reason is not None and reason.kind == "error"
        return self._stop_event is not None and self._stop_event.is_set()


@dataclass(frozen=True, slots=True)
class _StopReason:
    kind: str
    error: BaseException | None = None
    retryable: bool = False


class _StopController:
    """Atomically retain the first reason that stopped item scheduling."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._event = Event()
        self._reason: _StopReason | None = None

    @property
    def reason(self) -> _StopReason | None:
        with self._lock:
            return self._reason

    @property
    def stop_requested(self) -> bool:
        return self._event.is_set()

    def freeze(self, reason: _StopReason) -> bool:
        with self._lock:
            if self._reason is not None:
                return False
            self._reason = reason
            self._event.set()
            return True


class TransferEngine:
    """Run one persisted task through the durable transfer protocol.

    The task coordinator remains synchronous to its caller. Factory-created
    item workers may run concurrently, while each worker owns one complete,
    observable and resumable item lifecycle.
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
        item_worker_factory: ItemWorkerFactory | None = None,
        password: str | None = None,
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
        self._item_worker_factory = item_worker_factory
        self._password = password
        self._stop_scheduling = Event()
        self._active_run_lock = Lock()
        self._run_done = Event()
        self._run_done.set()
        self._active_token: CancellationToken | None = None
        self._futures_lock = Lock()
        self._active_futures: set[Future[ItemRunOutcome]] = set()
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
        if item_worker_factory is None and missing:
            raise TypeError("missing transfer services: " + ", ".join(missing))

    def shutdown(self, timeout_seconds: float = 2.0) -> bool:
        """Request a paused stop and wait for in-flight work to release resources.

        The engine never destroys a worker, closes a connection out from under
        one, or reports a timeout as a completed shutdown.  ``True`` means no
        run is in flight, or the in-flight ``run_task`` returned in time;
        ``False`` means "still stopping" and the caller must retry.

        The deadline is taken before anything else so ``timeout_seconds`` is an
        exact bound on the whole method, not just on the final wait.
        """
        deadline = monotonic() + max(0.0, timeout_seconds)
        self._stop_scheduling.set()
        with self._active_run_lock:
            token = self._active_token
            in_flight = not self._run_done.is_set()
        if token is not None:
            _request(token, "request_pause")
        with self._futures_lock:
            pending = tuple(self._active_futures)
        for future in pending:
            # Cancelling an already-running future is a no-op; this only
            # reclaims items that were queued but never started.
            future.cancel()
        if not in_flight:
            return True
        return self._run_done.wait(max(0.0, deadline - monotonic()))

    def run_task(self, task_id: TaskId, token: CancellationToken) -> TaskResult:
        """Run one task to a durable result on the calling thread.

        The engine serves one run at a time: ``_run_done``, ``_active_token``
        and ``_active_futures`` are published without an entry lock, and the
        ``shutdown`` latch is one-way and never reset.  Production honours this
        because engines are built per attempt and ``QueueCoordinator._run_lock``
        serialises calls; an engine instance is not reused after ``shutdown``.
        """
        with self._active_run_lock:
            self._run_done.clear()
            self._active_token = token
        try:
            return self._run_task(task_id, token)
        finally:
            with self._active_run_lock:
                self._active_token = None
                self._run_done.set()
            with self._futures_lock:
                self._active_futures.clear()

    def _run_task(self, task_id: TaskId, token: CancellationToken) -> TaskResult:
        task = self._repository.get_task(task_id)
        if _requested(token, "cancel_requested"):
            return self._finish(task, TaskState.CANCELED, False, kind="canceled")
        if self._stop_scheduling.is_set():
            _request(token, "request_pause")
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

        if self._item_worker_factory is None:
            for completed, item in enumerate(items, 1):
                result = self._run_item(item, task, token)
                if isinstance(result, TaskResult):
                    return result
                if result:
                    warnings.append(result)
                if self._stop_scheduling.is_set():
                    _request(token, "request_pause")
                if _requested(token, "cancel_requested"):
                    return self._finish(task, TaskState.CANCELED, False, completed, kind="canceled")
                if _requested(token, "pause_requested"):
                    return self._finish(task, TaskState.PAUSED, False, completed, kind="paused")
        else:
            files = [
                item for item in items
                if item.source_fingerprint.kind is SourceKind.FILE
            ]
            empty_directories = [
                item for item in items
                if item.source_fingerprint.kind is SourceKind.EMPTY_DIRECTORY
            ]
            completed, parallel_warnings, first_error, retryable, parallel_failed = (
                self._run_parallel_items(task, files, token)
            )
            warnings.extend(parallel_warnings)
            if first_error is not None:
                return self._fail_task(
                    task,
                    first_error,
                    None,
                    state=(
                        TaskState.WAITING_FOR_NETWORK if retryable else TaskState.FAILED
                    ),
                    completed=completed,
                    warnings=tuple(warnings),
                    failed=parallel_failed,
                )
            if _requested(token, "cancel_requested"):
                return self._finish(task, TaskState.CANCELED, False, completed, kind="canceled")
            if self._stop_scheduling.is_set():
                _request(token, "request_pause")
            if _requested(token, "pause_requested"):
                return self._finish(task, TaskState.PAUSED, False, completed, kind="paused")

            for item in empty_directories:
                if self._stop_scheduling.is_set():
                    _request(token, "request_pause")
                if _requested(token, "cancel_requested"):
                    return self._finish(task, TaskState.CANCELED, False, completed, kind="canceled")
                if _requested(token, "pause_requested"):
                    return self._finish(task, TaskState.PAUSED, False, completed, kind="paused")
                try:
                    outcome = self._run_worker(item, task, token)
                except BaseException as caught:  # noqa: BLE001 - worker boundary must be visible
                    outcome = self._unexpected_worker_failure(task, item, caught)
                if outcome.error is not None:
                    return self._fail_task(
                        task,
                        outcome.error,
                        item,
                        state=(
                            TaskState.WAITING_FOR_NETWORK
                            if outcome.retryable
                            else TaskState.FAILED
                        ),
                        completed=completed,
                        warnings=tuple(warnings),
                        failed=1,
                    )
                if outcome.state is ItemState.INTERRUPTED:
                    if _requested(token, "cancel_requested"):
                        return self._finish(
                            task, TaskState.CANCELED, False, completed, kind="canceled"
                        )
                    if _requested(token, "pause_requested"):
                        return self._finish(
                            task, TaskState.PAUSED, False, completed, kind="paused"
                        )
                    interruption_error = RuntimeError(
                        "item worker interrupted without a stop request"
                    )
                    self._publish(
                        TransferEvent(
                            task.id,
                            TaskState.RUNNING,
                            item.id,
                            interruption_error,
                            "failed",
                        )
                    )
                    return self._fail_task(
                        task,
                        interruption_error,
                        item,
                        completed=completed,
                        warnings=tuple(warnings),
                        failed=1,
                    )
                completed += 1
                if outcome.warning:
                    warnings.append(outcome.warning)
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

    def _run_parallel_items(
        self,
        task: TaskRecord,
        files: list[TransferItemRecord],
        token: CancellationToken,
    ) -> tuple[int, tuple[str, ...], BaseException | None, bool, int]:
        if not files:
            return 0, (), None, False, 0

        stop_controller = _StopController()
        worker_token = cast(
            CancellationToken,
            _WorkerCancellationToken(token, stop_controller),
        )
        active: dict[Future[ItemRunOutcome], TransferItemRecord] = {}
        outcomes: dict[TransferItemId, ItemRunOutcome] = {}
        next_index = 0

        with ThreadPoolExecutor(max_workers=task.connection.max_parallel_items) as executor:
            while active or next_index < len(files):
                self._freeze_user_stop(stop_controller, token)
                while (
                    not stop_controller.stop_requested
                    and not self._stop_scheduling.is_set()
                    and next_index < len(files)
                    and len(active) < task.connection.max_parallel_items
                ):
                    item = files[next_index]
                    next_index += 1
                    future = executor.submit(
                        self._run_worker,
                        item,
                        task,
                        worker_token,
                        stop_controller,
                    )
                    active[future] = item
                    self._track_future(future)
                if not active:
                    break

                done, _ = wait(active, return_when=FIRST_COMPLETED)
                completed_batch: list[tuple[TransferItemRecord, ItemRunOutcome]] = []
                for future in done:
                    item = active.pop(future)
                    self._untrack_future(future)
                    if future.cancelled():
                        # A shutdown cancelled this item before its thread ever
                        # started: it never ran, so it must not publish a
                        # failure event.  Leave its durable state untouched.
                        continue
                    try:
                        outcome = future.result()
                    except BaseException as caught:  # noqa: BLE001 - future failures must be durable
                        outcome = self._unexpected_worker_failure(task, item, caught)
                    outcomes[item.id] = outcome
                    completed_batch.append((item, outcome))

                # User actions have no timestamp. Their order is therefore the
                # next point where the coordinator can observe them, after every
                # future already returned by this wait call has been collected.
                self._freeze_user_stop(stop_controller, token)
                for item, outcome in completed_batch:
                    if (
                        outcome.error is None
                        and outcome.state is ItemState.INTERRUPTED
                        and not stop_controller.stop_requested
                    ):
                        interruption_error = RuntimeError(
                            "item worker interrupted without a stop request"
                        )
                        outcomes[item.id] = replace(
                            outcome,
                            error=interruption_error,
                        )
                        stop_controller.freeze(
                            _StopReason("error", interruption_error)
                        )
                        self._publish(
                            TransferEvent(
                                task.id,
                                TaskState.RUNNING,
                                item.id,
                                interruption_error,
                                "failed",
                            )
                        )

        completed = sum(
            outcome.error is None and outcome.state is not ItemState.INTERRUPTED
            for outcome in outcomes.values()
        )
        failed = sum(
            outcome.error is not None for outcome in outcomes.values()
        )
        ordered_warnings: list[str] = []
        for item in files:
            saved_outcome = outcomes.get(item.id)
            if saved_outcome is not None and saved_outcome.warning is not None:
                ordered_warnings.append(saved_outcome.warning)
        reason = stop_controller.reason
        if reason is not None and reason.kind == "error":
            return completed, tuple(ordered_warnings), reason.error, reason.retryable, failed
        return completed, tuple(ordered_warnings), None, False, failed

    @staticmethod
    def _freeze_user_stop(
        controller: _StopController,
        token: CancellationToken,
    ) -> None:
        if _requested(token, "cancel_requested"):
            controller.freeze(_StopReason("cancel"))
        elif _requested(token, "pause_requested"):
            controller.freeze(_StopReason("pause"))

    def _track_future(self, future: Future[ItemRunOutcome]) -> None:
        with self._futures_lock:
            self._active_futures.add(future)

    def _untrack_future(self, future: Future[ItemRunOutcome]) -> None:
        with self._futures_lock:
            self._active_futures.discard(future)

    def _run_worker(
        self,
        item: TransferItemRecord,
        task: TaskRecord,
        token: CancellationToken,
        stop_controller: _StopController | None = None,
    ) -> ItemRunOutcome:
        factory = self._item_worker_factory
        if factory is None:
            raise RuntimeError("item worker factory is unavailable")
        try:
            worker = factory(task, self._password or "")
        except BaseException as caught:  # noqa: BLE001 - factory failures must be classified
            retryable = self._retry_policy.is_retryable(caught)
            if stop_controller is not None:
                # Freeze the reason before publishing anything: a failing event
                # sink must not be able to let this failure escape unfrozen and
                # be misreported as a completed task.
                stop_controller.freeze(_StopReason("error", caught, retryable))
            return self._worker_construction_failure(task, item, caught, retryable)
        try:
            try:
                outcome = worker.run(item, task, token)
                if stop_controller is not None and outcome.error is not None:
                    stop_controller.freeze(
                        _StopReason("error", outcome.error, outcome.retryable)
                    )
            except BaseException as caught:
                if stop_controller is not None:
                    stop_controller.freeze(_StopReason("error", caught))
                raise
        finally:
            try:
                worker.close()
            except BaseException as caught:
                if stop_controller is not None:
                    stop_controller.freeze(_StopReason("error", caught))
                raise
        return outcome

    def _worker_construction_failure(
        self,
        task: TaskRecord,
        item: TransferItemRecord,
        error: BaseException,
        retryable: bool,
    ) -> ItemRunOutcome:
        """Classify a worker-factory failure like an item failure.

        A per-worker session is established inside the factory, so an
        unreachable NAS now surfaces here rather than at a runner-level
        connect.  The caller freezes the stop reason first and passes the
        resulting retryable classification in, so the classification decides
        whether the task waits for the network or fails permanently.
        """
        state = TaskState.WAITING_FOR_NETWORK if retryable else TaskState.FAILED
        self._publish(TransferEvent(task.id, state, item.id, error, "failed"))
        return ItemRunOutcome(item.id, item.state, error=error, retryable=retryable)

    def _unexpected_worker_failure(
        self,
        task: TaskRecord,
        item: TransferItemRecord,
        error: BaseException,
    ) -> ItemRunOutcome:
        self._publish(TransferEvent(task.id, TaskState.RUNNING, item.id, error, "failed"))
        return ItemRunOutcome(item.id, item.state, error=error, retryable=False)

    def _run_item(
        self, original_item: TransferItemRecord, task: TaskRecord, token: CancellationToken
    ) -> str | TaskResult | None:
        if self._recovery is None or self._writer is None or self._verifier is None or self._committer is None:
            raise RuntimeError("transfer services are unavailable")
        worker = TransferItemWorker(
            self._repository,
            self._recovery,
            self._writer,
            self._verifier,
            self._committer,
            self._deletion,
            smb_gateway=self._smb,
            session=self._session,
            event_sink=self._events,
            retry_policy=self._retry_policy,
            progress_tracker=self._progress,
            owns_resources=False,
        )
        outcome = worker.run(original_item, task, token)
        if outcome.error is not None:
            return self._fail_task(
                task, outcome.error, original_item,
                state=TaskState.WAITING_FOR_NETWORK if outcome.retryable else TaskState.FAILED,
                failed=1,
            )
        if outcome.state is ItemState.INTERRUPTED:
            if _requested(token, "cancel_requested"):
                return self._finish(task, TaskState.CANCELED, False, kind="canceled")
            if _requested(token, "pause_requested"):
                return self._finish(task, TaskState.PAUSED, False, kind="paused")
        return outcome.warning

    def _transition_task_if_needed(self, task: TaskRecord, target: TaskState) -> None:
        if task.state is target:
            return
        method = getattr(self._repository, "transition_task", None)
        if callable(method):
            method(task.id, task.state, target)

    def _fail_task(
        self,
        task: TaskRecord,
        error: BaseException,
        item: TransferItemRecord | None,
        *,
        state: TaskState = TaskState.FAILED,
        completed: int = 0,
        warnings: tuple[str, ...] = (),
        failed: int = 0,
    ) -> TaskResult:
        current = self._repository.get_task(task.id)
        if current.state is not state:
            self._transition_task_if_needed(current, state)
        self._publish(TransferEvent(task.id, state, None if item is None else item.id, error, "failed"))
        return TaskResult(
            False,
            state,
            error=error,
            warnings=warnings,
            completed_items=completed,
            failed_items=failed,
        )

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
        self.last_task_id: TaskId | None = None

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

    def release_thread_connection(self) -> None:
        release = getattr(self._repository, "release_thread_connection", None)
        if callable(release):
            release()

    def has_pending_tasks(self) -> bool:
        next_task = getattr(self._repository, "next_queued_task", None)
        if callable(next_task):
            return next_task() is not None
        with self._lifecycle_lock:
            return bool(self._queue)

    def request_pause(self) -> None:
        with self._lifecycle_lock:
            token = self._active_token
            if token is None:
                # No token is published yet, so latch the request onto the
                # next token published within this run.
                self._pending_pause = True
                return
        token.request_pause()

    def request_cancel(self) -> None:
        with self._lifecycle_lock:
            token = self._active_token
            if token is None:
                return
        token.request_cancel()

    def wait_for_safe_boundary(self, timeout: float) -> bool:
        """Wait until the current engine call returns after a safe block boundary."""
        return self._active_done.wait(timeout)

    def shutdown(self, timeout_seconds: float = 2.0) -> bool:
        """Stop the queue at a safe boundary and wait for the active engine.

        The single-active-task lock semantics are untouched: this only refuses
        new work and then asks the active engine to reach a safe boundary.
        """
        self.stop_accepting()
        self.request_pause()
        shutdown = getattr(self._engine, "shutdown", None)
        if callable(shutdown):
            return bool(shutdown(timeout_seconds))
        return self.wait_for_safe_boundary(timeout_seconds)

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
            self.last_task_id = None
            task_id: TaskId | None = None
            next_task = getattr(self._repository, "next_queued_task", None)
            if callable(next_task):
                task = next_task()
                task_id = None if task is None else task.id
                with self._lifecycle_lock:
                    if task_id in self._queue:
                        self._queue.remove(task_id)
            else:
                with self._lifecycle_lock:
                    task_id = self._queue.pop(0) if self._queue else None
                if task_id is None and self._repository is not None:
                    raise TypeError("queue repository must provide next_queued_task()")
            if task_id is None:
                return None
            self.last_task_id = task_id
            getter = getattr(self._repository, "get_task", None)
            if callable(getter):
                task = getter(task_id)
                if task.state in {TaskState.PAUSED, TaskState.CANCELED}:
                    return TaskResult(False, task.state, task_id=task_id)
            return replace(self._engine.run_task(task_id, active_token), task_id=task_id)
        finally:
            with self._lifecycle_lock:
                self._active_token = None
                self._active_done.set()
            self._run_lock.release()


__all__ = ["QueueCoordinator", "TaskResult", "TransferEngine", "TransferEvent"]
