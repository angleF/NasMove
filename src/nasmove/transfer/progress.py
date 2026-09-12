from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    total_bytes: int
    completed_bytes: int
    copied_bytes: int
    verified_bytes: int
    speed_bytes_per_second: float
    eta_seconds: float | None

    @property
    def total_work(self) -> int:
        return self.total_bytes

    @property
    def total_work_bytes(self) -> int:
        return self.total_bytes

    @property
    def completed_work(self) -> int:
        return self.completed_bytes

    @property
    def completed_work_bytes(self) -> int:
        return self.completed_bytes

    @property
    def speed(self) -> float:
        return self.speed_bytes_per_second

    @property
    def speed_bytes_per_sec(self) -> float:
        return self.speed_bytes_per_second

    @property
    def eta(self) -> float | None:
        return self.eta_seconds


class ProgressTracker:
    """Track copy plus remote-read work with bounded-rate notifications."""

    WINDOW_SECONDS = 30.0
    EVENT_INTERVAL_SECONDS = 0.25
    DATABASE_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        total_bytes: int = 0,
        *,
        verification_enabled: bool = True,
        full_verification: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        event_sink: Callable[[ProgressSnapshot], None] | None = None,
        database_sink: Callable[[ProgressSnapshot], None] | None = None,
    ) -> None:
        if type(total_bytes) is not int or total_bytes < 0:
            raise ValueError("total_bytes must be a non-negative integer")
        self._lock = RLock()
        self._source_bytes = total_bytes
        self._verification_enabled = verification_enabled if full_verification is None else full_verification
        self._clock = clock
        self._event_sink = event_sink
        self._database_sink = database_sink
        self._copied = 0
        self._verified = 0
        self._samples: deque[tuple[float, int]] = deque()
        self._last_event_at: float | None = None
        self._last_database_at: float | None = None
        self._pending_notifications: deque[tuple[ProgressSnapshot, bool, bool]] = deque()
        self._notifying = False

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._source_bytes * (2 if self._verification_enabled else 1)

    def set_total(self, total_bytes: int, *, verification_enabled: bool | None = None) -> None:
        if type(total_bytes) is not int or total_bytes < 0:
            raise ValueError("total_bytes must be a non-negative integer")
        with self._lock:
            self._source_bytes = total_bytes
            if verification_enabled is not None:
                self._verification_enabled = verification_enabled

    def record_copy(self, byte_count: int) -> ProgressSnapshot:
        with self._lock:
            self._add("copied", byte_count)
            snapshot = self._record(byte_count)
        return self._notify(snapshot)

    def record_verification(self, byte_count: int) -> ProgressSnapshot:
        with self._lock:
            self._add("verified", byte_count)
            snapshot = self._record(byte_count)
        return self._notify(snapshot)

    def update(self, *, copied_bytes: int | None = None, verified_bytes: int | None = None) -> ProgressSnapshot:
        with self._lock:
            before = self._copied + self._verified
            if copied_bytes is not None:
                self._set("copied", copied_bytes)
            if verified_bytes is not None:
                self._set("verified", verified_bytes)
            snapshot = self._record(max(0, self._copied + self._verified - before))
        return self._notify(snapshot)

    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            now = self._clock()
            self._trim_samples(now)
            return self._snapshot(now)

    def _record(self, delta: int) -> ProgressSnapshot:
        """Queue notifications in snapshot order while holding the state lock."""
        now = self._clock()
        self._samples.append((now, delta))
        self._trim_samples(now)
        snapshot = self._snapshot(now)
        event_due = self._last_event_at is None or now - self._last_event_at >= self.EVENT_INTERVAL_SECONDS
        database_due = (
            self._last_database_at is None
            or now - self._last_database_at >= self.DATABASE_INTERVAL_SECONDS
        )
        if event_due:
            self._last_event_at = now
        if database_due:
            self._last_database_at = now
        if event_due or database_due:
            self._pending_notifications.append((snapshot, event_due, database_due))
        return snapshot

    def _notify(self, snapshot: ProgressSnapshot) -> ProgressSnapshot:
        """Deliver callbacks outside the state lock, then report all failures."""
        with self._lock:
            if self._notifying:
                # Reentrant updates must not wait for the current callback.
                return snapshot
            self._notifying = True
        failures: list[BaseException] = []
        try:
            while True:
                with self._lock:
                    if not self._pending_notifications:
                        self._notifying = False
                        break
                    pending, event_due, database_due = self._pending_notifications.popleft()
                for sink, due in (
                    (self._event_sink, event_due), (self._database_sink, database_due)
                ):
                    if due:
                        try:
                            self._emit(sink, pending)
                        except BaseException as error:  # noqa: BLE001 - all failures are re-raised
                            # Finish this snapshot and drain reentrant updates
                            # before propagating callback failures to the caller.
                            failures.append(error)
        except BaseException:
            with self._lock:
                self._notifying = False
            raise
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("progress notification callbacks failed", failures)
        return snapshot

    def _snapshot(self, now: float) -> ProgressSnapshot:
        total = self.total_bytes
        completed = min(total, self._copied + self._verified)
        speed = 0.0
        eta: float | None = None
        if len(self._samples) >= 3:
            first_time = self._samples[0][0]
            elapsed = now - first_time
            if elapsed > 0:
                speed = sum(sample[1] for sample in self._samples) / elapsed
                if speed > 0:
                    eta = max(0.0, total - completed) / speed
        return ProgressSnapshot(total, completed, self._copied, self._verified, speed, eta)

    def _trim_samples(self, now: float) -> None:
        cutoff = now - self.WINDOW_SECONDS
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _add(self, field: str, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("progress increment must be a non-negative integer")
        setattr(self, "_" + field, getattr(self, "_" + field) + value)

    def _set(self, field: str, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("progress value must be a non-negative integer")
        setattr(self, "_" + field, value)

    @staticmethod
    def _emit(sink: Callable[[ProgressSnapshot], None] | object | None, snapshot: ProgressSnapshot) -> None:
        if sink is None:
            return
        if callable(sink):
            sink(snapshot)
            return
        publish = getattr(sink, "publish", None)
        if callable(publish):
            publish(snapshot)


__all__ = ["ProgressSnapshot", "ProgressTracker"]
