from __future__ import annotations

import math
from dataclasses import dataclass

from nasmove.core.errors import TransferErrorCategory, TransferFailure
from nasmove.smb.error_mapping import map_smb_error


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry schedule for transient network failures.

    ``jitter`` is a deterministic factor supplied by the caller, in the
    inclusive range ``[-0.2, 0.2]``.  Keeping the random draw outside this
    class makes the schedule straightforward to test and to reproduce in a
    diagnostic report.
    """

    delays: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 60.0, 60.0)
    jitter_limit: float = 0.2
    waiting_probe_seconds: float = 60.0

    def delay_seconds(self, attempt: int, jitter: float) -> float | None:
        if type(attempt) is not int or attempt <= 0:
            raise ValueError("attempt must be a positive integer")
        if not math.isfinite(jitter) or not -self.jitter_limit <= jitter <= self.jitter_limit:
            raise ValueError("jitter must be a finite factor between -0.2 and 0.2")
        if attempt > len(self.delays):
            return None
        return self.delays[attempt - 1] * (1.0 + jitter)

    @property
    def max_fast_retries(self) -> int:
        return len(self.delays)

    def is_retryable(self, error: BaseException | TransferFailure) -> bool:
        """Return whether an error is a transient network failure.

        Authentication, authorization, path, capacity and protocol errors
        are intentionally not retried.  ``TransferFailure`` is accepted so a
        caller that has already mapped an SMB exception does not need to map
        it again.
        """

        failure = error if isinstance(error, TransferFailure) else map_smb_error(error)
        return (
            failure.retryable
            and failure.category in {TransferErrorCategory.NETWORK, TransferErrorCategory.DNS}
        )

    def should_retry(self, error: BaseException | TransferFailure, attempt: int) -> bool:
        if type(attempt) is not int or attempt <= 0:
            raise ValueError("attempt must be a positive integer")
        return attempt <= self.max_fast_retries and self.is_retryable(error)


__all__ = ["RetryPolicy"]
