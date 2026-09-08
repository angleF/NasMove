from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, Protocol

from nasmove.core.model import TaskId
from nasmove.core.ports import SessionInfo
from nasmove.core.retry import RetryPolicy
from nasmove.core.states import TaskState
from nasmove.transfer.checkpoint_writer import CancellationToken
from nasmove.transfer.transfer_engine import TaskResult


class Engine(Protocol):
    def run_task(self, task_id: TaskId, token: CancellationToken) -> TaskResult: ...


class RetryingTaskRunner:
    """Own SMB reconnection and rerun the durable engine from safe checkpoints."""

    def __init__(
        self,
        repository: Any,
        smb_gateway: Any,
        credential_store: Any,
        engine_factory: Callable[[SessionInfo], Engine],
        *,
        retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(-0.2, 0.2),
    ) -> None:
        self._repository = repository
        self._smb = smb_gateway
        self._credentials = credential_store
        self._engine_factory = engine_factory
        self._policy = retry_policy or RetryPolicy()
        self._sleep = sleeper
        self._jitter = jitter

    def run_task(self, task_id: TaskId, token: CancellationToken) -> TaskResult:
        attempt = 0
        while True:
            interrupted = self._requested_result(task_id, token)
            if interrupted is not None:
                return interrupted
            task = self._repository.get_task(task_id)
            password = self._credentials.get_password(task.connection.profile_id)
            if not password:
                return self._finish(task_id, TaskState.PAUSED)
            if task.state is TaskState.QUEUED:
                self._repository.transition_task(task_id, TaskState.QUEUED, TaskState.RUNNING)
                task = self._repository.get_task(task_id)
            try:
                session = self._smb.connect(task.connection, password)
            except Exception as error:  # noqa: BLE001 - SMB errors are classified below
                if not self._policy.is_retryable(error):
                    return self._finish(task_id, TaskState.FAILED, error)
                self._move_to_waiting(task_id)
                attempt += 1
                self._smb.reset_connection()
                self._wait(attempt)
                continue

            result = self._engine_factory(session).run_task(task_id, token)
            if result.state is not TaskState.WAITING_FOR_NETWORK:
                return result
            if result.error is None or not self._policy.is_retryable(result.error):
                return result
            attempt += 1
            self._smb.reset_connection()
            self._wait(attempt)

    def _wait(self, attempt: int) -> None:
        delay = self._policy.delay_seconds(attempt, self._jitter())
        self._sleep(self._policy.waiting_probe_seconds if delay is None else delay)

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


__all__ = ["RetryingTaskRunner"]
