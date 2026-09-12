from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from nasmove.transfer.progress import ProgressTracker


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_full_verification_counts_copy_and_remote_read_work() -> None:
    clock = Clock()
    tracker = ProgressTracker(total_bytes=100, verification_enabled=True, clock=clock)

    tracker.record_copy(40)
    clock.value = 1.0
    tracker.record_verification(20)

    snapshot = tracker.snapshot()
    assert snapshot.total_bytes == 200
    assert snapshot.completed_bytes == 60
    assert snapshot.copied_bytes == 40
    assert snapshot.verified_bytes == 20


def test_eta_is_unknown_until_three_samples_and_uses_thirty_second_window() -> None:
    clock = Clock()
    tracker = ProgressTracker(total_bytes=100, verification_enabled=False, clock=clock)

    for value in (10, 20):
        tracker.record_copy(value)
        clock.value += 1
    assert tracker.snapshot().eta_seconds is None

    tracker.record_copy(30)
    clock.value += 1
    snapshot = tracker.snapshot()
    assert snapshot.eta_seconds is not None
    assert snapshot.speed_bytes_per_second > 0

    clock.value = 40.0
    tracker.record_copy(1)
    assert tracker.snapshot().speed_bytes_per_second <= 2


def test_progress_events_and_database_updates_are_throttled() -> None:
    clock = Clock()
    events = []
    database_updates = []
    tracker = ProgressTracker(
        total_bytes=100,
        clock=clock,
        event_sink=events.append,
        database_sink=database_updates.append,
    )

    tracker.record_copy(1)
    clock.value = 0.1
    tracker.record_copy(1)
    clock.value = 0.3
    tracker.record_copy(1)
    clock.value = 1.1
    tracker.record_copy(1)

    assert len(events) == 3
    assert len(database_updates) == 2


def test_progress_tracker_totals_parallel_copy_and_verification() -> None:
    tracker = ProgressTracker(400)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: tracker.record_copy(100), range(4)))
        list(pool.map(lambda _: tracker.record_verification(100), range(4)))

    snapshot = tracker.snapshot()
    assert snapshot.copied_bytes == 400
    assert snapshot.verified_bytes == 400
    assert snapshot.completed_bytes == 800


@pytest.mark.parametrize("operation", ["record_copy", "record_verification", "update", "snapshot"])
def test_progress_operation_keeps_its_snapshot_consistent_during_parallel_update(operation) -> None:
    clock_entered = Event()
    release_clock = Event()
    update_started = Event()
    update_finished = Event()

    def clock() -> float:
        if not clock_entered.is_set():
            clock_entered.set()
            assert release_clock.wait(2)
        return 0.0

    tracker = ProgressTracker(100, clock=clock)

    def read_progress():
        if operation == "update":
            return tracker.update(copied_bytes=10)
        if operation == "snapshot":
            return tracker.snapshot()
        return getattr(tracker, operation)(10)

    def change_progress() -> None:
        update_started.set()
        tracker.set_total(200, verification_enabled=False)
        tracker.update(copied_bytes=50, verified_bytes=20)
        update_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(read_progress)
        try:
            assert clock_entered.wait(2)
            changing = pool.submit(change_progress)
            assert update_started.wait(2)
            update_finished.wait(0.05)
        finally:
            release_clock.set()
        snapshot = reading.result(timeout=2)
        changing.result(timeout=2)

    assert snapshot.total_bytes == 200
    assert snapshot.copied_bytes == (10 if operation in {"record_copy", "update"} else 0)
    assert snapshot.verified_bytes == (10 if operation == "record_verification" else 0)
    assert tracker.snapshot().copied_bytes == 50


@pytest.mark.parametrize("sink_name", ["event_sink", "database_sink"])
def test_progress_callback_can_read_tracker_from_another_thread(sink_name) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        observed = []

        def sink(snapshot) -> None:
            observed.append(pool.submit(tracker.snapshot).result(timeout=2))

        tracker = ProgressTracker(100, **{sink_name: sink})
        tracker.record_copy(10)

    assert observed[0].copied_bytes == 10


@pytest.mark.parametrize("sink_name", ["event", "database"])
def test_progress_notifications_remain_ordered_when_first_publisher_is_delayed(
    sink_name, monkeypatch
) -> None:
    first_snapshot_ready = Event()
    release_first_publisher = Event()
    clock = Clock()
    observed = {"event": [], "database": []}
    tracker = ProgressTracker(
        100,
        clock=clock,
        event_sink=lambda snapshot: observed["event"].append(snapshot.copied_bytes),
        database_sink=lambda snapshot: observed["database"].append(snapshot.copied_bytes),
    )
    notify = tracker._notify

    def delay_first_publisher(snapshot, *args):
        if snapshot.copied_bytes == 10:
            first_snapshot_ready.set()
            assert release_first_publisher.wait(2)
        return notify(snapshot, *args)

    monkeypatch.setattr(tracker, "_notify", delay_first_publisher)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(tracker.record_copy, 10)
        try:
            assert first_snapshot_ready.wait(2)
            clock.value = 1.0
            second = pool.submit(tracker.record_copy, 10)
            assert second.result(timeout=2).copied_bytes == 20
        finally:
            release_first_publisher.set()
        assert first.result(timeout=2).copied_bytes == 10

    assert observed[sink_name] == [10, 20]


@pytest.mark.parametrize("cross_thread", [False, True])
def test_progress_callback_can_update_tracker_without_reordering_either_sink(cross_thread) -> None:
    clock = Clock()
    events = []
    database_updates = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        def event_sink(snapshot) -> None:
            events.append(snapshot.copied_bytes)
            if snapshot.copied_bytes == 10:
                clock.value = 1.0
                if cross_thread:
                    pool.submit(tracker.record_copy, 10).result(timeout=2)
                else:
                    tracker.record_copy(10)

        tracker = ProgressTracker(
            100,
            clock=clock,
            event_sink=event_sink,
            database_sink=lambda snapshot: database_updates.append(snapshot.copied_bytes),
        )
        tracker.record_copy(10)

    assert events == [10, 20]
    assert database_updates == [10, 20]


def test_progress_notifications_continue_after_a_sink_raises() -> None:
    clock = Clock()
    events = []
    database_updates = []

    def event_sink(snapshot) -> None:
        events.append(snapshot.copied_bytes)
        if snapshot.copied_bytes == 10:
            raise RuntimeError("event sink failed")

    tracker = ProgressTracker(
        100,
        clock=clock,
        event_sink=event_sink,
        database_sink=lambda snapshot: database_updates.append(snapshot.copied_bytes),
    )
    with pytest.raises(RuntimeError, match="event sink failed"):
        tracker.record_copy(10)
    clock.value = 1.0
    tracker.record_copy(10)

    assert events == [10, 20]
    assert database_updates == [10, 20]


@pytest.mark.parametrize("sink_name", ["event", "database"])
@pytest.mark.parametrize("cross_thread", [False, True])
def test_progress_drains_reentrant_notifications_before_reraising_sink_error(
    sink_name, cross_thread
) -> None:
    clock = Clock()
    observed = {"event": [], "database": []}
    failure = RuntimeError("sink failed after updating progress")
    with ThreadPoolExecutor(max_workers=1) as pool:
        def publish(name, snapshot) -> None:
            observed[name].append(snapshot.copied_bytes)
            if name == sink_name and snapshot.copied_bytes == 10:
                clock.value = 1.0
                if cross_thread:
                    pool.submit(tracker.record_copy, 10).result(timeout=2)
                else:
                    tracker.record_copy(10)
                raise failure

        tracker = ProgressTracker(
            100,
            clock=clock,
            event_sink=lambda snapshot: publish("event", snapshot),
            database_sink=lambda snapshot: publish("database", snapshot),
        )
        with pytest.raises(RuntimeError) as raised:
            tracker.record_copy(10)

    assert raised.value is failure
    assert tracker.snapshot().copied_bytes == 20
    assert observed == {"event": [10, 20], "database": [10, 20]}
    assert not tracker._pending_notifications
    assert tracker._notifying is False


def test_progress_reports_every_sink_failure_after_draining_reentrant_notifications() -> None:
    clock = Clock()
    events = []
    database_updates = []
    event_error = RuntimeError("event sink failed")
    database_error = ValueError("database sink failed")

    def event_sink(snapshot) -> None:
        events.append(snapshot.copied_bytes)
        if snapshot.copied_bytes == 10:
            clock.value = 1.0
            tracker.record_copy(10)
            raise event_error

    def database_sink(snapshot) -> None:
        database_updates.append(snapshot.copied_bytes)
        if snapshot.copied_bytes == 10:
            raise database_error

    tracker = ProgressTracker(
        100, clock=clock, event_sink=event_sink, database_sink=database_sink
    )
    with pytest.raises(ExceptionGroup) as raised:
        tracker.record_copy(10)

    assert raised.value.exceptions == (event_error, database_error)
    assert events == [10, 20]
    assert database_updates == [10, 20]
    assert not tracker._pending_notifications
    assert tracker._notifying is False
