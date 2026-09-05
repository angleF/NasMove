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
