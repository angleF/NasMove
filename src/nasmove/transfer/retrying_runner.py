from __future__ import annotations

import random
import time
from collections.abc import Callable
from threading import Event, Lock
from time import monotonic
from typing import Any, Protocol

from nasmove.core.model import TaskId, TaskRecord
from nasmove.core.retry import RetryPolicy
from nasmove.core.states import TaskState
from nasmove.transfer.checkpoint_writer import CancellationToken
from nasmove.transfer.transfer_engine import TaskResult, TransferEvent


class Engine(Protocol):
    def run_task(self, task_id: TaskId, token: CancellationToken) -> TaskResult: ...

    def shutdown(self, timeout_seconds: float = 2.0) -> bool: ...


EngineFactory = Callable[[TaskRecord, str], Engine]


class RetryingTaskRunner:
    """Own network retry scheduling and rerun the durable engine from checkpoints.

    The runner no longer owns an SMB session.  Each attempt builds a fresh
    engine through ``engine_factory(task, password)`` so that per-worker
    connections are established inside the engine, and a rebuilt engine
    replaces every session that a network interruption invalidated.
    """

    def __init__(
        self,
        repository: Any,
        credential_store: Any,
        engine_factory: EngineFactory,
        *,
        retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(-0.2, 0.2),
        event_sink: Any = None,
    ) -> None:
        self._repository = repository
        self._credentials = credential_store
        self._engine_factory = engine_factory
        self._policy = retry_policy or RetryPolicy()
        self._sleep = sleeper
        # The production default sleeps uninterruptibly, which would make a
        # shutdown wait out a 60 s backoff while the caller holds the GUI
        # thread.  Only the default is replaced with a latch-aware wait; an
        # injected sleeper keeps the test seam's exact behaviour.
        self._interruptible_wait = sleeper is time.sleep
        self._jitter = jitter
        self._events = event_sink
        self._stop_scheduling = Event()
        self._active_lock = Lock()
        self._active_engine: Engine | None = None
        self._run_done = Event()
        self._run_done.set()

    def shutdown(self, timeout_seconds: float = 2.0) -> bool:
        """Latch "no further attempts" and wait for the in-flight run to return.

        The latch is observed at the top of ``run_task``'s attempt loop, so a
        shutdown that lands before an attempt or between two attempts makes the
        run stop as ``PAUSED`` instead of starting another engine.

        The in-flight per-attempt engine is delegated to so the engine-level
        contract (cancel unstarted items, honest ``False``) is reached from
        production.  ``True`` means no run is in flight, or the run returned
        within ``timeout_seconds``; ``False`` means "still stopping" - nothing
        is destroyed, and the caller must retry.  A run that is live but
        momentarily outside an attempt (for example during a retry wait) still
        reports ``False`` rather than a shutdown it cannot back.
        """
        deadline = monotonic() + max(0.0, timeout_seconds)
        self._stop_scheduling.set()
        with self._active_lock:
            engine = self._active_engine
            in_flight = not self._run_done.is_set()
        if engine is not None:
            delegate = getattr(engine, "shutdown", None)
            if callable(delegate):
                delegate(max(0.0, deadline - monotonic()))
        if not in_flight:
            return True
        return self._run_done.wait(max(0.0, deadline - monotonic()))

    def run_task(self, task_id: TaskId, token: CancellationToken) -> TaskResult:
        with self._active_lock:
            self._run_done.clear()
        try:
            return self._run_task(task_id, token)
        finally:
            with self._active_lock:
                self._active_engine = None
                self._run_done.set()

    def _run_task(self, task_id: TaskId, token: CancellationToken) -> TaskResult:
        attempt = 0
        while True:
            interrupted = self._requested_result(task_id, token)
            if interrupted is not None:
                return interrupted
            if self._stop_scheduling.is_set():
                # A shutdown landed before this attempt, or between attempts.
                # Never build another engine, and never walk to COMPLETED.
                token.request_pause()
                return self._finish(task_id, TaskState.PAUSED)
            task = self._repository.get_task(task_id)
            if self._events is not None:
                self._events.publish(TransferEvent(task_id, task.state))
            password = self._credentials.get_password(task.connection.profile_id)
            if not password:
                return self._finish(task_id, TaskState.PAUSED)
            if task.state is TaskState.QUEUED:
                self._repository.transition_task(task_id, TaskState.QUEUED, TaskState.RUNNING)
                task = self._repository.get_task(task_id)
            try:
                engine = self._engine_factory(task, password)
            except Exception as error:  # noqa: BLE001 - SMB errors are classified below
                if not self._policy.is_retryable(error):
                    return self._finish(task_id, TaskState.FAILED, error)
                self._move_to_waiting(task_id)
                attempt += 1
                self._wait(attempt, task_id)
                continue

            with self._active_lock:
                self._active_engine = engine
            try:
                result = engine.run_task(task_id, token)
            finally:
                with self._active_lock:
                    self._active_engine = None
            if result.state is not TaskState.WAITING_FOR_NETWORK:
                return result
            if result.error is None or not self._policy.is_retryable(result.error):
                return result
            attempt += 1
            self._wait(attempt, task_id)

    def _wait(self, attempt: int, task_id: TaskId | None = None) -> None:
        delay = self._policy.delay_seconds(attempt, self._jitter())
        wait = self._policy.waiting_probe_seconds if delay is None else delay
        if self._events is not None and task_id is not None:
            self._events.publish(TransferEvent(task_id, TaskState.WAITING_FOR_NETWORK,
                kind="retry", retry_attempt=attempt, retry_delay=wait))
        if self._interruptible_wait:
            # ``_stop_scheduling`` is the one-way shutdown latch, so waiting on
            # it wakes ``shutdown`` promptly without changing the retry schedule.
            self._stop_scheduling.wait(wait)
            return
        self._sleep(wait)

    def _move_to_waiting(self, task_id: TaskId) -> None:
        task = self._repository.get_task(task_id)
        if task.state is not TaskState.WAITING_FOR_NETWORK:
            self._repository.transition_task(task_id, task.state, TaskState.WAITING_FOR_NETWORK)

    def _finish(
        self,
        task_id: TaskId,
        state: TaskState,
        error: BaseException | None = None,
    ) -> TaskResult:
        task = self._repository.get_task(task_id)
        if task.state is not state:
            self._repository.transition_task(task_id, task.state, state)
        return TaskResult(False, state, error=error)

    def _requested_result(
        self,
        task_id: TaskId,
        token: CancellationToken,
    ) -> TaskResult | None:
        if token.cancel_requested:
            return self._finish(task_id, TaskState.CANCELED)
        if token.pause_requested:
            return self._finish(task_id, TaskState.PAUSED)
        return None


__all__ = ["Engine", "EngineFactory", "RetryingTaskRunner"]
