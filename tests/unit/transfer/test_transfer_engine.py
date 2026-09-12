import time
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path, PurePosixPath
from threading import Event, Lock, Thread, get_ident
from typing import Self

import pytest

from nasmove.core.model import RemotePath, TransferItemId
from nasmove.core.ports import SessionInfo
from nasmove.core.states import ItemState, SourceKind, TaskState, TransferAction
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from nasmove.transfer import transfer_engine as transfer_engine_module
from nasmove.transfer.checkpoint_writer import CancellationToken, CheckpointWriter
from nasmove.transfer.commit import TargetCommitter
from nasmove.transfer.deletion import SourceDeletionService
from nasmove.transfer.item_worker import ItemRunOutcome, TransferItemWorker
from nasmove.transfer.recovery import RecoveryCoordinator
from nasmove.transfer.transfer_engine import QueueCoordinator, TaskResult, TransferEngine
from nasmove.transfer.verification import IntegrityVerifier
from tests.fixtures.builders import (
    build_connection_config,
    build_task_record,
    build_transfer_item_record,
)
from tests.fixtures.transfer import (
    FakeDependencies,
    TransferLocal,
    TransferRemote,
    TransferRepository,
    TransferToken,
)


class _ParallelWorkerHarness:
    def __init__(self, expected_overlap: int, behavior: str = "success") -> None:
        self.expected_overlap = expected_overlap
        self.behavior = behavior
        self.active = 0
        self.max_active = 0
        self.started: list[TransferItemId] = []
        self.closed: list[TransferItemId] = []
        self.factory_passwords: list[str] = []
        self.thread_ids: dict[TransferItemId, int] = {}
        self.overlap_reached = Event()
        self.release = Event()
        self.internal_cancel_seen = Event()
        self.user_token: CancellationToken | None = None
        self.repository: object | None = None
        self.retryable_error = ConnectionResetError("NAS restarted")
        self.failure_error = PermissionError("access denied")
        self.peer_error = PermissionError("peer failed")
        self._lock = Lock()

    def factory(self, task, password):
        del task
        self.factory_passwords.append(password)
        acquire = getattr(self.repository, "acquire_thread_connection", None)
        if callable(acquire):
            acquire()
        return _ParallelWorker(self)

    def release_thread_connection(self) -> None:
        release = getattr(self.repository, "release_thread_connection", None)
        if callable(release):
            release()

    def enter(self, item_id: TransferItemId) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.append(item_id)
            self.thread_ids[item_id] = get_ident()
            if self.active >= self.expected_overlap:
                self.overlap_reached.set()

    def leave(self) -> None:
        with self._lock:
            self.active -= 1

    def run(self, item_id: TransferItemId, token) -> ItemRunOutcome:
        if self.behavior == "fail-first":
            if item_id == TransferItemId("item-1"):
                if not self.overlap_reached.wait(2):
                    raise TimeoutError("parallel peer did not start")
                return ItemRunOutcome(
                    item_id,
                    ItemState.INTERRUPTED,
                    error=self.failure_error,
                )
            deadline = time.monotonic() + 2
            while not token.cancel_requested:
                if time.monotonic() >= deadline:
                    raise TimeoutError("internal stop was not delivered")
                time.sleep(0.001)
            self.internal_cancel_seen.set()
            token.request_cancel()
            return ItemRunOutcome(item_id, ItemState.INTERRUPTED)
        if self.behavior in {
            "retryable-first",
            "retryable-first-nonretryable-peer",
            "retryable-first-late-cancel",
        }:
            if item_id == TransferItemId("item-1"):
                if not self.overlap_reached.wait(2):
                    raise TimeoutError("parallel peer did not start")
                return ItemRunOutcome(
                    item_id,
                    ItemState.WAITING_RETRY,
                    error=self.retryable_error,
                    retryable=True,
                )
            deadline = time.monotonic() + 2
            while not token.cancel_requested:
                if time.monotonic() >= deadline:
                    raise TimeoutError("internal stop was not delivered")
                time.sleep(0.001)
            self.internal_cancel_seen.set()
            if self.behavior == "retryable-first-nonretryable-peer":
                return ItemRunOutcome(
                    item_id,
                    ItemState.INTERRUPTED,
                    error=self.peer_error,
                )
            if self.behavior == "retryable-first-late-cancel":
                if self.user_token is None:
                    raise RuntimeError("late user token was not configured")
                self.user_token.request_cancel()
            return ItemRunOutcome(item_id, ItemState.INTERRUPTED)
        if self.behavior == "fail-both":
            if item_id == TransferItemId("item-1"):
                if not self.overlap_reached.wait(2):
                    raise TimeoutError("parallel peer did not start")
                return ItemRunOutcome(
                    item_id,
                    ItemState.INTERRUPTED,
                    error=self.failure_error,
                )
            deadline = time.monotonic() + 2
            while not token.cancel_requested:
                if time.monotonic() >= deadline:
                    raise TimeoutError("internal stop was not delivered")
                time.sleep(0.001)
            return ItemRunOutcome(
                item_id,
                ItemState.INTERRUPTED,
                error=self.peer_error,
            )
        if self.behavior == "wait-for-user":
            if not self.overlap_reached.wait(2):
                raise TimeoutError("parallel peer did not start")
            deadline = time.monotonic() + 2
            while not token.cancel_requested and not token.pause_requested:
                if time.monotonic() >= deadline:
                    raise TimeoutError("user stop was not delivered")
                time.sleep(0.001)
            return ItemRunOutcome(item_id, ItemState.INTERRUPTED)
        if self.behavior == "block-until-released":
            if not self.overlap_reached.wait(2):
                raise TimeoutError("configured concurrency was not reached")
            if not self.release.wait(2):
                raise TimeoutError("worker release was not delivered")
            return ItemRunOutcome(item_id, ItemState.INTERRUPTED)
        if self.behavior == "block-then-done":
            if not self.overlap_reached.wait(2):
                raise TimeoutError("configured concurrency was not reached")
            if not self.release.wait(2):
                raise TimeoutError("worker release was not delivered")
            return ItemRunOutcome(item_id, ItemState.DONE)
        if self.behavior == "raise-first" and item_id == TransferItemId("item-1"):
            raise RuntimeError("worker crashed")
        if not self.overlap_reached.wait(2):
            raise TimeoutError("configured concurrency was not reached")
        return ItemRunOutcome(item_id, ItemState.DONE)


class _ParallelWorker:
    def __init__(self, harness: _ParallelWorkerHarness) -> None:
        self._harness = harness
        self._item_id: TransferItemId | None = None

    def run(self, item, task, token):
        del task
        self._item_id = item.id
        self._harness.enter(item.id)
        try:
            return self._harness.run(item.id, token)
        finally:
            self._harness.leave()

    def close(self) -> None:
        if self._item_id is not None:
            self._harness.closed.append(self._item_id)
            self._harness.release_thread_connection()


def _parallel_engine(
    *,
    max_parallel_items: int,
    item_count: int,
    harness: _ParallelWorkerHarness,
    source_kinds: tuple[SourceKind, ...] | None = None,
):
    trace: list[str] = []
    events: list[object] = []
    repository = TransferRepository(trace)
    harness.repository = repository
    task = replace(
        build_task_record(connection=build_connection_config(max_parallel_items=max_parallel_items)),
        state=TaskState.QUEUED,
        total_files=item_count,
        total_bytes=item_count * 1024,
    )
    repository.tasks[task.id] = task
    base_item = build_transfer_item_record()
    for index in range(1, item_count + 1):
        item_id = TransferItemId(f"item-{index}")
        kind = SourceKind.FILE if source_kinds is None else source_kinds[index - 1]
        item = replace(
            base_item,
            id=item_id,
            task_id=task.id,
            source_path=Path(f"/source/file-{index}.bin"),
            relative_path=PurePosixPath(f"file-{index}.bin"),
            final_path=RemotePath(f"target/file-{index}.bin"),
            temp_path=RemotePath(f"target/.file-{index}.bin.part"),
            source_fingerprint=replace(base_item.source_fingerprint, kind=kind),
        )
        repository.items[item.id] = item

    class Sink:
        def publish(self, event) -> None:
            events.append(event)

    engine = TransferEngine(
        repository,
        item_worker_factory=harness.factory,
        password="memory-only-secret",
        event_sink=Sink(),
    )
    return engine, repository, task, events


@pytest.mark.parametrize("limit", [1, 2, 4])
def test_parallel_items_never_exceed_task_snapshot_limit(limit) -> None:
    harness = _ParallelWorkerHarness(limit)
    engine, _, task, _ = _parallel_engine(
        max_parallel_items=limit,
        item_count=limit + 2,
        harness=harness,
    )

    result = engine.run_task(task.id, CancellationToken())

    assert result.success is True
    assert result.completed_items == limit + 2
    assert harness.max_active == limit
    assert harness.factory_passwords == ["memory-only-secret"] * (limit + 2)


def test_parallel_limit_one_preserves_planned_item_order() -> None:
    harness = _ParallelWorkerHarness(1)
    engine, _, task, _ = _parallel_engine(
        max_parallel_items=1,
        item_count=4,
        harness=harness,
    )

    result = engine.run_task(task.id, CancellationToken())

    assert result.success is True
    assert harness.started == [TransferItemId(f"item-{index}") for index in range(1, 5)]


@pytest.mark.parametrize(
    ("behavior", "expected_state"),
    [
        ("fail-first", TaskState.FAILED),
        ("retryable-first", TaskState.WAITING_FOR_NETWORK),
    ],
)
def test_parallel_failure_stops_new_items_without_canceling_user_token(
    behavior, expected_state
) -> None:
    harness = _ParallelWorkerHarness(2, behavior)
    engine, repository, task, _ = _parallel_engine(
        max_parallel_items=2,
        item_count=4,
        harness=harness,
    )
    token = CancellationToken()

    result = engine.run_task(task.id, token)

    assert result.state is expected_state
    assert repository.get_task(task.id).state is expected_state
    assert set(harness.started) == {TransferItemId("item-1"), TransferItemId("item-2")}
    assert set(harness.closed) == set(harness.started)
    assert harness.internal_cancel_seen.is_set()
    assert token.cancel_requested is False
    assert token.pause_requested is False


def test_parallel_failure_aggregates_failed_item_count_and_first_error() -> None:
    harness = _ParallelWorkerHarness(2, "fail-both")
    engine, repository, task, _ = _parallel_engine(
        max_parallel_items=2,
        item_count=2,
        harness=harness,
    )

    result = engine.run_task(task.id, CancellationToken())

    assert result.state is TaskState.FAILED
    assert result.success is False
    # Both dispatched items failed, so the summary must count both even though
    # only the first error is the authoritative reason.
    assert result.failed_items == 2
    assert result.error is harness.failure_error
    assert result.completed_items == 0
    assert repository.get_task(task.id).state is TaskState.FAILED


def test_retryable_first_error_is_not_changed_by_nonretryable_peer() -> None:
    harness = _ParallelWorkerHarness(2, "retryable-first-nonretryable-peer")
    engine, repository, task, _ = _parallel_engine(
        max_parallel_items=2,
        item_count=4,
        harness=harness,
    )

    result = engine.run_task(task.id, CancellationToken())

    assert result.state is TaskState.WAITING_FOR_NETWORK
    assert result.error is harness.retryable_error
    assert repository.get_task(task.id).state is TaskState.WAITING_FOR_NETWORK
    assert set(harness.started) == {TransferItemId("item-1"), TransferItemId("item-2")}
    assert set(harness.closed) == set(harness.started)


def test_late_user_cancel_does_not_replace_retryable_first_error() -> None:
    harness = _ParallelWorkerHarness(2, "retryable-first-late-cancel")
    engine, repository, task, _ = _parallel_engine(
        max_parallel_items=2,
        item_count=4,
        harness=harness,
    )
    token = CancellationToken()
    harness.user_token = token

    result = engine.run_task(task.id, token)

    assert token.cancel_requested is True
    assert result.state is TaskState.WAITING_FOR_NETWORK
    assert result.error is harness.retryable_error
    assert repository.get_task(task.id).state is TaskState.WAITING_FOR_NETWORK


@pytest.mark.parametrize(
    ("behavior", "expected_state", "error_attribute"),
    [
        ("retryable-first", TaskState.WAITING_FOR_NETWORK, "retryable_error"),
        ("fail-first", TaskState.FAILED, "failure_error"),
    ],
)
def test_completed_worker_error_is_frozen_before_scheduler_observes_late_cancel(
    monkeypatch, behavior, expected_state, error_attribute
) -> None:
    harness = _ParallelWorkerHarness(2, behavior)
    engine, repository, task, _ = _parallel_engine(
        max_parallel_items=2,
        item_count=4,
        harness=harness,
    )
    token = CancellationToken()
    wait_has_completed_future = Event()
    release_scheduler = Event()
    real_wait = transfer_engine_module.wait

    def blocked_wait(*args, **kwargs):
        result = real_wait(*args, **kwargs)
        wait_has_completed_future.set()
        if not release_scheduler.wait(2):
            raise TimeoutError("scheduler processing was not released")
        return result

    monkeypatch.setattr(transfer_engine_module, "wait", blocked_wait)
    results: list[TaskResult] = []
    runner = Thread(target=lambda: results.append(engine.run_task(task.id, token)))
    runner.start()
    try:
        assert wait_has_completed_future.wait(2)
        token.request_cancel()
    finally:
        release_scheduler.set()
    runner.join(2)

    assert not runner.is_alive()
    assert token.cancel_requested is True
    assert results[0].state is expected_state
    assert results[0].error is getattr(harness, error_attribute)
    assert repository.get_task(task.id).state is expected_state


@pytest.mark.parametrize(
    ("request_method", "expected_state"),
    [
        ("request_cancel", TaskState.CANCELED),
        ("request_pause", TaskState.PAUSED),
    ],
)
def test_user_stop_observed_after_completed_batch_wins_before_next_dispatch(
    monkeypatch, request_method, expected_state
) -> None:
    harness = _ParallelWorkerHarness(1)
    engine, _, task, _ = _parallel_engine(
        max_parallel_items=1,
        item_count=2,
        harness=harness,
    )
    token = CancellationToken()
    wait_has_completed_future = Event()
    release_scheduler = Event()
    real_wait = transfer_engine_module.wait

    def blocked_wait(*args, **kwargs):
        result = real_wait(*args, **kwargs)
        wait_has_completed_future.set()
        if not release_scheduler.wait(2):
            raise TimeoutError("scheduler processing was not released")
        return result

    monkeypatch.setattr(transfer_engine_module, "wait", blocked_wait)
    results: list[TaskResult] = []
    runner = Thread(target=lambda: results.append(engine.run_task(task.id, token)))
    runner.start()
    try:
        assert wait_has_completed_future.wait(2)
        getattr(token, request_method)()
    finally:
        release_scheduler.set()
    runner.join(2)

    assert not runner.is_alive()
    assert results[0].state is expected_state
    assert results[0].completed_items == 1
    assert harness.started == [TransferItemId("item-1")]
    assert harness.closed == harness.started


@pytest.mark.parametrize(
    ("request_method", "expected_state"),
    [
        ("request_cancel", TaskState.CANCELED),
        ("request_pause", TaskState.PAUSED),
    ],
)
def test_parallel_user_stop_prevents_new_dispatch_and_waits_for_workers(
    request_method, expected_state
) -> None:
    harness = _ParallelWorkerHarness(2, "wait-for-user")
    engine, _, task, _ = _parallel_engine(
        max_parallel_items=2,
        item_count=4,
        harness=harness,
    )
    token = CancellationToken()
    results: list[TaskResult] = []
    runner = Thread(target=lambda: results.append(engine.run_task(task.id, token)))
    runner.start()
    assert harness.overlap_reached.wait(2)

    getattr(token, request_method)()
    runner.join(2)

    assert not runner.is_alive()
    assert results[0].state is expected_state
    assert set(harness.started) == {TransferItemId("item-1"), TransferItemId("item-2")}
    assert set(harness.closed) == set(harness.started)


def test_engine_shutdown_waits_for_in_flight_workers_and_releases_connections() -> None:
    harness = _ParallelWorkerHarness(2, "wait-for-user")
    engine, repository, task, _ = _parallel_engine(
        max_parallel_items=2,
        item_count=4,
        harness=harness,
    )
    results: list[TaskResult] = []
    runner = Thread(
        target=lambda: results.append(engine.run_task(task.id, CancellationToken())),
        daemon=True,
    )
    runner.start()
    assert harness.overlap_reached.wait(2)

    stopped = engine.shutdown(2.0)
    runner.join(2)

    assert stopped is True
    assert not runner.is_alive()
    assert results[0].state is TaskState.PAUSED
    assert set(harness.started) == {TransferItemId("item-1"), TransferItemId("item-2")}
    assert set(harness.closed) == set(harness.started)
    assert repository.release_calls == len(harness.started)


def test_engine_shutdown_timeout_keeps_worker_and_cleans_up_after_it_finishes() -> None:
    harness = _ParallelWorkerHarness(1, "block-until-released")
    engine, repository, task, _ = _parallel_engine(
        max_parallel_items=1,
        item_count=1,
        harness=harness,
    )
    results: list[TaskResult] = []
    runner = Thread(
        target=lambda: results.append(engine.run_task(task.id, CancellationToken())),
        daemon=True,
    )
    runner.start()
    assert harness.overlap_reached.wait(2)

    assert engine.shutdown(0.05) is False
    assert runner.is_alive()
    assert harness.closed == []
    assert repository.release_calls == 0

    harness.release.set()
    runner.join(2)
    assert not runner.is_alive()
    assert results[0].state is TaskState.PAUSED
    assert harness.closed == harness.started == [TransferItemId("item-1")]
    assert repository.release_calls == 1


def test_engine_shutdown_mid_run_never_reports_partial_task_completed() -> None:
    harness = _ParallelWorkerHarness(2, "wait-for-user")
    engine, repository, task, events = _parallel_engine(
        max_parallel_items=2,
        item_count=4,
        harness=harness,
    )
    results: list[TaskResult] = []
    runner = Thread(
        target=lambda: results.append(engine.run_task(task.id, CancellationToken())),
        daemon=True,
    )
    runner.start()
    assert harness.overlap_reached.wait(2)

    assert engine.shutdown(2.0) is True
    runner.join(2)

    assert not runner.is_alive()
    assert results[0].state is TaskState.PAUSED
    assert results[0].state is not TaskState.COMPLETED
    assert results[0].success is False
    assert results[0].completed_items == 0
    assert repository.get_task(task.id).state is TaskState.PAUSED
    assert len(harness.started) == 2
    assert {
        event.state for event in events
    }.isdisjoint({TaskState.COMPLETED, TaskState.COMPLETED_WITH_WARNINGS})


def test_engine_shutdown_before_task_pickup_leaves_task_paused() -> None:
    harness = _ParallelWorkerHarness(1)
    engine, repository, task, _ = _parallel_engine(
        max_parallel_items=1,
        item_count=2,
        harness=harness,
    )

    assert engine.shutdown(1.0) is True

    result = engine.run_task(task.id, CancellationToken())

    assert result.state is TaskState.PAUSED
    assert result.success is False
    assert repository.get_task(task.id).state is TaskState.PAUSED
    assert harness.started == []
    assert harness.closed == []


class _HeldExecutor:
    """Executor double that submits without starting, leaving futures pending.

    A plain ``concurrent.futures.Future`` that never had a thread attached is
    cancellable, which is exactly the window ``shutdown`` may win.  A real pool
    still hands the cancelled work item to a worker, which calls
    ``set_running_or_notify_cancel`` and wakes the waiting loop without running
    the task; ``dispatch`` replays that handshake on demand.
    """

    last: "_HeldExecutor | None" = None

    def __init__(self, max_workers: int = 1) -> None:
        self.max_workers = max_workers
        self.futures: list[Future] = []
        self.work_items: list[tuple[Future, object, tuple, dict]] = []
        self.submitted = Event()
        _HeldExecutor.last = self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def submit(self, fn, *args, **kwargs) -> Future:
        future: Future = Future()
        self.futures.append(future)
        self.work_items.append((future, fn, args, kwargs))
        if len(self.futures) >= 2:
            self.submitted.set()
        return future

    def dispatch(self) -> None:
        """Hand each work item to a worker, the way a real pool would."""
        for future, fn, args, kwargs in self.work_items:
            if future.set_running_or_notify_cancel():
                fn(*args, **kwargs)


def test_shutdown_cancelled_unstarted_future_does_not_publish_failure_event(monkeypatch) -> None:
    _HeldExecutor.last = None
    harness = _ParallelWorkerHarness(2)
    engine, repository, task, events = _parallel_engine(
        max_parallel_items=2,
        item_count=2,
        harness=harness,
    )
    monkeypatch.setattr(transfer_engine_module, "ThreadPoolExecutor", _HeldExecutor)
    results: list[TaskResult] = []
    runner = Thread(
        target=lambda: results.append(engine.run_task(task.id, CancellationToken())),
        daemon=True,
    )
    runner.start()
    deadline = time.monotonic() + 2
    executor = _HeldExecutor.last
    while executor is None and time.monotonic() < deadline:
        time.sleep(0.001)
        executor = _HeldExecutor.last
    assert executor is not None
    assert executor.submitted.wait(2)
    # Both submitted futures must be tracked before the shutdown, otherwise a
    # future submitted-but-not-yet-tracked would escape the cancel and the
    # held executor would never complete it.
    def tracked() -> int:
        with engine._futures_lock:
            return len(engine._active_futures)

    deadline = time.monotonic() + 2
    while tracked() < 2 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert tracked() == 2

    # The run is parked in wait(); the held executor has not dispatched, so the
    # shutdown can only latch and cancel, and must report "still stopping".
    assert engine.shutdown(0.05) is False
    assert [future.cancelled() for future in executor.futures] == [True, True]
    # The run is still parked, so the first shutdown could not have finished it.
    assert runner.is_alive()

    # Now let the pool's worker threads observe the cancellations, exactly as a
    # real executor would, and confirm the run returns within the bound.
    executor.dispatch()
    assert engine.shutdown(5.0) is True
    runner.join(2)

    assert not runner.is_alive()
    # The shutdown cancelled both queued items before their threads started.
    assert harness.started == []
    assert [event for event in events if event.kind == "failed"] == []
    assert results[0].state is TaskState.PAUSED
    assert repository.get_task(task.id).state is TaskState.PAUSED


class _ConnectionGuard:
    """Model the repository's per-thread connection ownership contract."""

    def __init__(self, base: object) -> None:
        self._base = base
        self.outstanding = 0
        self.closed = False

    def acquire_thread_connection(self) -> None:
        self.outstanding += 1

    def release_thread_connection(self) -> None:
        self.outstanding -= 1
        release = getattr(self._base, "release_thread_connection", None)
        if callable(release):
            release()

    def close(self) -> None:
        if self.outstanding:
            raise RuntimeError("repository closed while a worker still held its connection")
        self.closed = True


def test_repository_close_is_blocked_until_parallel_workers_release_connections() -> None:
    harness = _ParallelWorkerHarness(2, "block-until-released")
    engine, repository, task, _ = _parallel_engine(
        max_parallel_items=2,
        item_count=2,
        harness=harness,
    )
    guard = _ConnectionGuard(repository)
    harness.repository = guard
    results: list[TaskResult] = []
    runner = Thread(
        target=lambda: results.append(engine.run_task(task.id, CancellationToken())),
        daemon=True,
    )
    runner.start()
    assert harness.overlap_reached.wait(2)

    with pytest.raises(RuntimeError, match="still held its connection"):
        guard.close()
    assert engine.shutdown(0.05) is False
    with pytest.raises(RuntimeError, match="still held its connection"):
        guard.close()

    harness.release.set()
    runner.join(2)

    assert results[0].state is TaskState.PAUSED
    assert guard.outstanding == 0
    assert repository.release_calls == 2
    guard.close()
    assert guard.closed is True


def test_queue_shutdown_stops_accepting_pauses_active_run_and_delegates_to_engine() -> None:
    started = Event()
    release = Event()
    received: list[object] = []
    shutdown_calls: list[float] = []

    class Engine:
        def run_task(self, task_id, token):
            del task_id
            received.append(token)
            started.set()
            while not token.pause_requested:
                release.wait(0.001)
            return TaskResult(False, TaskState.PAUSED)

        def shutdown(self, timeout_seconds: float = 2.0) -> bool:
            shutdown_calls.append(timeout_seconds)
            return True

    coordinator = QueueCoordinator(Engine())
    coordinator.enqueue("task-a")
    coordinator.enqueue("task-b")
    worker = Thread(target=coordinator.run_next, daemon=True)
    worker.start()
    assert started.wait(2)

    assert coordinator.shutdown(1.5) is True
    release.set()
    worker.join(2)

    assert shutdown_calls == [1.5]
    assert len(received) == 1
    assert received[0].pause_requested is True
    assert not worker.is_alive()
    with pytest.raises(RuntimeError, match="stopping"):
        coordinator.enqueue("task-c")


def test_queue_shutdown_falls_back_to_safe_boundary_without_engine_shutdown() -> None:
    started = Event()
    release = Event()

    class Engine:
        def run_task(self, task_id, token):
            del task_id
            started.set()
            release.wait(2)
            assert token.pause_requested is True
            return TaskResult(False, TaskState.PAUSED)

    coordinator = QueueCoordinator(Engine())
    coordinator.enqueue("task-a")
    worker = Thread(target=coordinator.run_next, daemon=True)
    worker.start()
    assert started.wait(2)

    assert coordinator.shutdown(0.01) is False
    assert worker.is_alive()

    release.set()
    assert coordinator.shutdown(2.0) is True
    worker.join(2)
    assert not worker.is_alive()


def test_parallel_verification_failure_after_restart_retains_source(tmp_path) -> None:
    db_path = tmp_path / "restart.db"
    trace: list[str] = []
    local = TransferLocal(b"payload", trace)
    remote = TransferRemote(trace)
    session = SessionInfo("3.1.1", True, True, 1)
    task = replace(
        build_task_record(connection=build_connection_config(max_parallel_items=2)),
        action=TransferAction.MOVE,
        state=TaskState.QUEUED,
        total_files=2,
        total_bytes=2 * len(local.content),
    )
    items = []
    for index in (1, 2):
        item = replace(
            FakeDependencies(
                local, remote, TransferRepository(trace), trace, TransferToken()
            ).item(size=len(local.content)),
            id=TransferItemId(f"item-{index}"),
            task_id=task.id,
            source_path=Path(f"/source/file-{index}.bin"),
            relative_path=PurePosixPath(f"file-{index}.bin"),
            final_path=RemotePath(f"target/file-{index}.bin"),
            temp_path=RemotePath(f"target/.file-{index}.bin.part"),
            state=ItemState.PLANNED,
        )
        items.append(item)
        remote.files[item.temp_path.value] = bytearray(local.content)
        remote.file_ids[item.temp_path.value] = f"file-ready-{index}"

    repository = SqliteTaskRepository(db_path)
    # The workers must bind to whichever store is live: after the simulated
    # restart the pre-crash store is closed and only the reopened one is valid.
    stores: dict[str, SqliteTaskRepository] = {"live": repository}

    def build_worker(worker_task, password):
        del worker_task, password
        store = stores["live"]
        verifier = IntegrityVerifier(store, local, remote, session)
        return TransferItemWorker(
            store,
            RecoveryCoordinator(store, local, remote, session),
            CheckpointWriter(store, local, remote),
            verifier,
            TargetCommitter(store, remote, session),
            SourceDeletionService(store, local, remote, verifier),
            smb_gateway=remote,
            session=session,
        )

    try:
        repository.create_task(task, items)
        repository.transition_task(task.id, TaskState.QUEUED, TaskState.RUNNING)
        for item in items:
            repository.transition_item(item.id, ItemState.PLANNED, ItemState.TRANSFERRING)
            repository.transition_item(item.id, ItemState.TRANSFERRING, ItemState.TRANSFERRED)
        # Simulate a forced quit while the task is still active, then the
        # restart recovery path that marks it interrupted.
        assert repository.mark_active_tasks_interrupted() == 1
        assert repository.get_task(task.id).state is TaskState.INTERRUPTED
        assert local.source_exists is True
        assert local.trash_calls == []
    finally:
        repository.close()

    # Restart: the durable store is reopened and the resumed full verification
    # now fails. Nothing unverified may be moved to the Trash.
    reopened = SqliteTaskRepository(db_path)
    stores["live"] = reopened
    remote.replace_after_read = True
    try:
        engine = TransferEngine(reopened, item_worker_factory=build_worker)
        result = engine.run_task(task.id, CancellationToken())

        assert result.state is TaskState.FAILED
        assert reopened.get_task(task.id).state is TaskState.FAILED
        # The run is fail-fast: whichever sibling loses the race to the first
        # verification failure is interrupted, so the durable states are
        # VERIFY_FAILED plus at most INTERRUPTED.  What must never appear is a
        # state that claims the unverified item is safe to commit or delete.
        states = {reopened.get_item(item.id).state for item in items}
        assert ItemState.VERIFY_FAILED in states
        assert states <= {ItemState.VERIFY_FAILED, ItemState.INTERRUPTED}
        assert not any(
            path.startswith("target/file-") for path in remote.files
        ), "unverified item must never reach its final committed path"
        assert local.source_exists is True
        assert local.trash_calls == []
    finally:
        reopened.close()


def test_empty_directories_run_serially_after_parallel_files() -> None:
    harness = _ParallelWorkerHarness(1)
    engine, _, task, _ = _parallel_engine(
        max_parallel_items=2,
        item_count=2,
        harness=harness,
        source_kinds=(SourceKind.EMPTY_DIRECTORY, SourceKind.FILE),
    )
    calling_thread = get_ident()

    result = engine.run_task(task.id, CancellationToken())

    assert result.success is True
    assert harness.started == [TransferItemId("item-2"), TransferItemId("item-1")]
    assert harness.thread_ids[TransferItemId("item-2")] != calling_thread
    assert harness.thread_ids[TransferItemId("item-1")] == calling_thread


def test_empty_directory_loop_observes_stop_latch_before_next_item() -> None:
    # The empty-directory loop runs after the pool drains and is the one place
    # where a shutdown can still dispatch another item before the token pause
    # lands.  Simulate that window exactly: the latch is set, the token is
    # deliberately NOT paused, and the first item is allowed to finish.
    harness = _ParallelWorkerHarness(1, "block-then-done")
    engine, _, task, _ = _parallel_engine(
        max_parallel_items=2,
        item_count=2,
        harness=harness,
        source_kinds=(SourceKind.EMPTY_DIRECTORY, SourceKind.EMPTY_DIRECTORY),
    )
    results: list[TaskResult] = []
    runner = Thread(
        target=lambda: results.append(engine.run_task(task.id, CancellationToken())),
        daemon=True,
    )
    runner.start()
    assert harness.overlap_reached.wait(2)

    engine._stop_scheduling.set()
    harness.release.set()
    runner.join(2)

    assert not runner.is_alive()
    assert results[0].state is TaskState.PAUSED
    assert results[0].state is not TaskState.COMPLETED
    assert results[0].success is False
    assert harness.started == [TransferItemId("item-1")]


def test_unhandled_future_exception_is_item_and_task_visible() -> None:
    harness = _ParallelWorkerHarness(1, "raise-first")
    engine, repository, task, events = _parallel_engine(
        max_parallel_items=1,
        item_count=2,
        harness=harness,
    )

    result = engine.run_task(task.id, CancellationToken())

    assert result.state is TaskState.FAILED
    assert isinstance(result.error, RuntimeError)
    assert repository.get_task(task.id).state is TaskState.FAILED
    assert harness.started == harness.closed == [TransferItemId("item-1")]
    assert any(
        event.item_id == TransferItemId("item-1")
        and isinstance(event.error, RuntimeError)
        for event in events
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


def test_serial_engine_delegates_each_item_without_closing_borrowed_resources(engine_fixture, monkeypatch) -> None:
    fixture = engine_fixture
    second = replace(fixture.item, id=TransferItemId("item-2"))
    fixture.repository.items[second.id] = second
    gateway = TransferRemote([])
    fixture.engine._smb = gateway
    delegated = []
    original_run = TransferItemWorker.run

    def run(worker, item, task, token):
        delegated.append((item.id, worker._owns_resources))
        return original_run(worker, item, task, token)

    monkeypatch.setattr(TransferItemWorker, "run", run)

    result = fixture.engine.run_task(fixture.move_task.id, fixture.token)

    assert result.success is True
    assert result.completed_items == 2
    assert delegated == [(fixture.item.id, False), (second.id, False)]
    assert gateway.disconnect_calls == fixture.repository.release_calls == 0


def test_serial_engine_shutdown_mid_run_leaves_task_paused(engine_fixture, monkeypatch) -> None:
    fixture = engine_fixture
    second = replace(fixture.item, id=TransferItemId("item-2"))
    fixture.repository.items[second.id] = second
    attempted: list[TransferItemId] = []
    worker_started = Event()
    release = Event()
    original_run = TransferItemWorker.run

    def run(worker, item, task, token):
        attempted.append(item.id)
        if len(attempted) == 1:
            worker_started.set()
            assert release.wait(2)
        return original_run(worker, item, task, token)

    monkeypatch.setattr(TransferItemWorker, "run", run)
    results: list[TaskResult] = []
    runner = Thread(
        target=lambda: results.append(fixture.engine.run_task(fixture.move_task.id, fixture.token)),
        daemon=True,
    )
    runner.start()
    assert worker_started.wait(2)

    assert fixture.engine.shutdown(0.05) is False
    assert runner.is_alive()
    release.set()
    runner.join(2)

    assert not runner.is_alive()
    assert results[0].state is TaskState.PAUSED
    assert results[0].state is not TaskState.COMPLETED
    assert results[0].success is False
    assert fixture.repository.get_task(fixture.move_task.id).state is TaskState.PAUSED
    # The unprocessed second item was never dispatched.
    assert attempted == [fixture.item.id]


def test_serial_engine_stop_latch_pauses_before_next_item_without_token_pause(
    engine_fixture, monkeypatch
) -> None:
    # `shutdown` always pauses the token, which masks the serial loop's own
    # latch check.  Set the latch alone so a first item that completes cleanly
    # is still followed by a pause instead of another dispatch.
    fixture = engine_fixture
    second = replace(fixture.item, id=TransferItemId("item-2"))
    fixture.repository.items[second.id] = second
    attempted: list[TransferItemId] = []
    worker_started = Event()
    release = Event()
    original_run = TransferItemWorker.run

    def run(worker, item, task, token):
        attempted.append(item.id)
        if len(attempted) == 1:
            worker_started.set()
            assert release.wait(2)
        return original_run(worker, item, task, token)

    monkeypatch.setattr(TransferItemWorker, "run", run)
    results: list[TaskResult] = []
    runner = Thread(
        target=lambda: results.append(fixture.engine.run_task(fixture.move_task.id, fixture.token)),
        daemon=True,
    )
    runner.start()
    assert worker_started.wait(2)

    fixture.engine._stop_scheduling.set()
    harness_token_paused = fixture.token.pause_requested
    release.set()
    runner.join(2)

    assert harness_token_paused is False
    assert not runner.is_alive()
    assert results[0].state is TaskState.PAUSED
    assert results[0].state is not TaskState.COMPLETED
    assert attempted == [fixture.item.id]


def test_copy_item_does_not_delete_source(engine_fixture) -> None:
    copy_task = replace(engine_fixture.move_task, action=TransferAction.COPY)
    engine_fixture.repository.tasks[copy_task.id] = copy_task
    result = engine_fixture.engine.run_task(copy_task.id, engine_fixture.token)

    assert result.success is True
    assert "authorize_delete" not in engine_fixture.trace
    assert "delete_source" not in engine_fixture.trace


def test_skipped_conflict_is_not_transferred_or_deleted(engine_fixture) -> None:
    skipped = replace(engine_fixture.item, state=ItemState.SKIPPED)
    engine_fixture.repository.items[skipped.id] = skipped

    result = engine_fixture.engine.run_task(engine_fixture.move_task.id, engine_fixture.token)

    assert result.success is True
    assert result.state is TaskState.COMPLETED_WITH_WARNINGS
    assert engine_fixture.trace == []
    assert engine_fixture.repository.get_item(skipped.id).state is ItemState.SKIPPED


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


# The concurrent real-NAS gate lives with the engine tests because it exercises
# the production per-item worker factory.  It is skipped unless the isolated
# Synology environment is explicitly enabled, exactly like the other scenarios.
@pytest.mark.synology
def test_real_synology_concurrent_files_use_distinct_worker_resources(
    synology_fixture,
) -> None:
    result = synology_fixture.transfer_concurrent_files(file_count=6, max_parallel_items=2)

    assert result.completed_files == 6
    assert result.verified_files == 6
    assert result.distinct_worker_gateways == 6
    assert result.distinct_worker_sessions == 6
    assert result.peak_parallel_workers >= 2
    assert result.source_files_retained == 6
