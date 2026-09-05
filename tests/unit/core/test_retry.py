from __future__ import annotations

import math

import pytest

from nasmove.core.retry import RetryPolicy
from nasmove.smb.error_mapping import StaleSmbHandleError, UnsupportedSmbDialectError, map_smb_error


def test_retry_schedule_without_jitter() -> None:
    policy = RetryPolicy()
    assert [policy.delay_seconds(index, jitter=0.0) for index in range(1, 9)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
        30.0,
        60.0,
        60.0,
    ]
    assert policy.delay_seconds(9, jitter=0.0) is None


@pytest.mark.parametrize(("jitter", "factor"), [(-0.2, 0.8), (0.2, 1.2)])
def test_retry_schedule_applies_twenty_percent_jitter(jitter: float, factor: float) -> None:
    assert RetryPolicy().delay_seconds(1, jitter=jitter) == pytest.approx(factor)


def test_retry_rejects_invalid_attempt_and_jitter() -> None:
    policy = RetryPolicy()
    with pytest.raises(ValueError):
        policy.delay_seconds(0, jitter=0.0)
    with pytest.raises(ValueError):
        policy.delay_seconds(1, jitter=0.21)
    with pytest.raises(ValueError):
        policy.delay_seconds(1, jitter=math.nan)


@pytest.mark.parametrize(
    "error",
    [PermissionError("denied"), FileNotFoundError("missing"), OSError(28, "full"), UnsupportedSmbDialectError()],
)
def test_only_transient_network_errors_are_retryable(error: Exception) -> None:
    policy = RetryPolicy()
    assert policy.is_retryable(error) is False
    assert policy.should_retry(error, attempt=1) is False


def test_eighth_network_failure_enters_waiting_probe_interval() -> None:
    policy = RetryPolicy()
    error = TimeoutError("temporary network timeout")
    assert policy.should_retry(error, attempt=8) is True
    assert policy.delay_seconds(8, jitter=0.0) == 60.0
    assert policy.should_retry(error, attempt=9) is False
    assert policy.waiting_probe_seconds == 60.0


def test_stale_smb_handle_is_a_retryable_network_failure() -> None:
    failure = map_smb_error(StaleSmbHandleError("session invalidated"))
    assert failure.category.value == "network"
    assert failure.retryable is True
    assert RetryPolicy().should_retry(StaleSmbHandleError("session invalidated"), attempt=1) is True
