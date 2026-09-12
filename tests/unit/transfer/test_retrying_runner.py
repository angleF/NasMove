import sqlite3
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from time import monotonic

import pytest

from nasmove.core.model import ConnectionConfig, TaskId
from nasmove.core.states import TaskState
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from nasmove.security.sqlite_store import SqliteCredentialStore
from nasmove.transfer.checkpoint_writer import CancellationToken
from nasmove.transfer.retrying_runner import RetryingTaskRunner
from nasmove.transfer.transfer_engine import TaskResult, TransferEngine
from tests.fixtures.builders import (
    build_connection_config,
    build_task_record,
    build_transfer_item_record,
)
from tests.fixtures.transfer import TransferRepository


class Repository:
    def __init__(self) -> None:
        self.task = replace(build_task_record(), state=TaskState.QUEUED)

    def get_task(self, task_id):
        assert task_id == self.task.id
        return self.task

    def transition_task(self, task_id, expected, target) -> None:
        assert task_id == self.task.id
        assert self.task.state is expected
        self.task = replace(self.task, state=target)


class Credentials:
    def get_password(self, profile_id):
        assert str(profile_id) == "connection-profile-1"
        return "memory-only-secret"


def test_runner_rebuilds_engine_with_task_and_keychain_password_after_network_wait() -> None:
    repository = Repository()
    calls: list[tuple[TaskId, str]] = []
    results = [
        TaskResult(False, TaskState.WAITING_FOR_NETWORK, error=ConnectionResetError()),
        TaskResult(True, TaskState.COMPLETED),
    ]
    sleeps: list[float] = []

    class ResultsEngine:
        def run_task(self, task_id: TaskId, token: CancellationToken) -> TaskResult:
            del task_id, token
            result = results.pop(0)
            repository.task = replace(repository.task, state=result.state)
            return result

    def engine_factory(task, password):
        calls.append((task.id, password))
        return ResultsEngine()

    runner = RetryingTaskRunner(
        repository,
        Credentials(),
        engine_factory,
        retry_policy=_policy(delays=(1.0,)),
        sleeper=sleeps.append,
        jitter=lambda: 0.0,
    )

    result = runner.run_task(repository.task.id, CancellationToken())

    assert result.state is TaskState.COMPLETED
    assert calls == [(TaskId("task-1"), "memory-only-secret")] * 2
    assert sleeps == [1.0]


def test_runner_waits_at_probe_interval_after_fast_retries() -> None:
    repository = Repository()
    attempts = 0
    passwords: list[str] = []
    sleeps: list[float] = []

    def engine_factory(task, password):
        del task
        passwords.append(password)

        class Engine:
            def run_task(self, task_id, token):
                nonlocal attempts
                del task_id, token
                attempts += 1
                if attempts < 4:
                    repository.task = replace(
                        repository.task,
                        state=TaskState.WAITING_FOR_NETWORK,
                    )
                    return TaskResult(
                        False,
                        TaskState.WAITING_FOR_NETWORK,
                        error=ConnectionResetError(),
                    )
                repository.task = replace(repository.task, state=TaskState.COMPLETED)
                return TaskResult(True, TaskState.COMPLETED)

        return Engine()

    runner = RetryingTaskRunner(
        repository,
        Credentials(),
        engine_factory,
        retry_policy=_policy(delays=(1.0, 2.0), waiting_probe_seconds=60.0),
        sleeper=sleeps.append,
        jitter=lambda: 0.0,
    )

    result = runner.run_task(repository.task.id, CancellationToken())

    assert result.success is True
    assert sleeps == [1.0, 2.0, 60.0]
    assert passwords == ["memory-only-secret"] * 4


def test_runner_stops_retrying_non_retryable_engine_factory_failure() -> None:
    repository = Repository()
    calls: list[tuple[TaskId, str]] = []

    def engine_factory(task, password):
        calls.append((task.id, password))
        raise PermissionError("denied")

    runner = RetryingTaskRunner(
        repository,
        Credentials(),
        engine_factory,
        sleeper=lambda _delay: None,
        jitter=lambda: 0.0,
    )

    result = runner.run_task(repository.task.id, CancellationToken())

    assert result.success is False
    assert result.state is TaskState.FAILED
    assert repository.task.state is TaskState.FAILED
    assert calls == [(TaskId("task-1"), "memory-only-secret")]


def test_runner_rebuilds_engine_after_retryable_engine_factory_failure() -> None:
    repository = Repository()
    attempts = 0
    sleeps: list[float] = []

    def engine_factory(task, password):
        nonlocal attempts
        del task, password
        attempts += 1
        if attempts == 1:
            raise ConnectionResetError("NAS restarted")

        class Engine:
            def run_task(self, task_id, token):
                del task_id, token
                repository.task = replace(repository.task, state=TaskState.COMPLETED)
                return TaskResult(True, TaskState.COMPLETED)

        return Engine()

    runner = RetryingTaskRunner(
        repository,
        Credentials(),
        engine_factory,
        retry_policy=_policy(delays=(1.0,)),
        sleeper=sleeps.append,
        jitter=lambda: 0.0,
    )

    result = runner.run_task(repository.task.id, CancellationToken())

    assert result.state is TaskState.COMPLETED
    assert attempts == 2
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    ("request_method", "expected_state"),
    [("request_cancel", TaskState.CANCELED), ("request_pause", TaskState.PAUSED)],
)
def test_runner_short_circuits_user_stop_before_building_engine(
    request_method: str,
    expected_state: TaskState,
) -> None:
    repository = Repository()
    calls: list[object] = []

    def engine_factory(task, password):
        calls.append((task, password))
        raise AssertionError("engine must not be built after a user stop request")

    runner = RetryingTaskRunner(
        repository,
        Credentials(),
        engine_factory,
        sleeper=lambda _delay: None,
        jitter=lambda: 0.0,
    )
    token = CancellationToken()
    getattr(token, request_method)()

    result = runner.run_task(repository.task.id, token)

    assert result.state is expected_state
    assert repository.task.state is expected_state
    assert calls == []


def test_runner_shutdown_before_attempt_pauses_without_building_engine() -> None:
    repository = Repository()
    built: list[object] = []

    def engine_factory(task, password):
        built.append((task, password))
        raise AssertionError("engine must not be built after shutdown")

    runner = RetryingTaskRunner(
        repository,
        Credentials(),
        engine_factory,
        sleeper=lambda _delay: None,
        jitter=lambda: 0.0,
    )

    assert runner.shutdown(1.0) is True
    result = runner.run_task(repository.task.id, CancellationToken())

    assert result.state is TaskState.PAUSED
    assert result.success is False
    assert repository.task.state is TaskState.PAUSED
    assert built == []


def test_runner_shutdown_delegates_to_active_engine_and_waits() -> None:
    repository = Repository()
    started = Event()
    release = Event()
    delegated: list[float] = []

    class BlockingEngine:
        def run_task(self, task_id, token):
            del task_id, token
            started.set()
            assert release.wait(2)
            repository.task = replace(repository.task, state=TaskState.PAUSED)
            return TaskResult(False, TaskState.PAUSED)

        def shutdown(self, timeout_seconds: float = 2.0) -> bool:
            delegated.append(timeout_seconds)
            release.set()
            return True

    runner = RetryingTaskRunner(
        repository,
        Credentials(),
        lambda task, password: BlockingEngine(),
        sleeper=lambda _delay: None,
        jitter=lambda: 0.0,
    )

    results: list[TaskResult] = []
    thread = Thread(
        target=lambda: results.append(runner.run_task(repository.task.id, CancellationToken())),
        daemon=True,
    )
    thread.start()
    assert started.wait(2)

    assert runner.shutdown(1.5) is True
    thread.join(2)

    assert not thread.is_alive()
    assert len(delegated) == 1
    # The delegate receives the remaining budget, bounded by the caller's timeout.
    assert 0.0 <= delegated[0] <= 1.5
    assert results[0].state is TaskState.PAUSED


def test_runner_shutdown_reports_false_while_sleeping_between_attempts() -> None:
    repository = Repository()
    attempts = 0
    entered_sleep = Event()
    release_sleep = Event()

    def sleeper(_delay: float) -> None:
        entered_sleep.set()
        assert release_sleep.wait(2)

    def engine_factory(task, password):
        del task, password
        nonlocal attempts
        attempts += 1

        class Engine:
            def run_task(self, task_id, token):
                del task_id, token
                repository.task = replace(
                    repository.task, state=TaskState.WAITING_FOR_NETWORK
                )
                return TaskResult(
                    False,
                    TaskState.WAITING_FOR_NETWORK,
                    error=ConnectionResetError("NAS restarted"),
                )

        return Engine()

    runner = RetryingTaskRunner(
        repository,
        Credentials(),
        engine_factory,
        retry_policy=_policy(delays=(1.0,)),
        sleeper=sleeper,
        jitter=lambda: 0.0,
    )

    results: list[TaskResult] = []
    thread = Thread(
        target=lambda: results.append(runner.run_task(repository.task.id, CancellationToken())),
        daemon=True,
    )
    thread.start()
    assert entered_sleep.wait(2)

    # Between attempts the runner owns no engine but is still live, so it must
    # not claim the shutdown succeeded.
    assert runner.shutdown(0.05) is False
    assert thread.is_alive()

    release_sleep.set()
    thread.join(2)

    assert not thread.is_alive()
    # The latch ends the run as PAUSED instead of launching another attempt.
    assert results[0].state is TaskState.PAUSED
    assert attempts == 1


def test_runner_shutdown_interrupts_default_backoff_wait_promptly() -> None:
    repository = Repository()
    retry_published = Event()

    class Sink:
        def publish(self, event: object) -> None:
            if getattr(event, "kind", None) == "retry":
                retry_published.set()

    def engine_factory(task, password):
        del task, password

        class Engine:
            def run_task(self, task_id, token):
                del task_id, token
                repository.task = replace(
                    repository.task, state=TaskState.WAITING_FOR_NETWORK
                )
                return TaskResult(
                    False,
                    TaskState.WAITING_FOR_NETWORK,
                    error=ConnectionResetError("NAS restarted"),
                )

        return Engine()

    # The production default sleeper must stay interruptible: a 60 s backoff
    # cannot block a window close for its full duration.
    runner = RetryingTaskRunner(
        repository,
        Credentials(),
        engine_factory,
        retry_policy=_policy(delays=(60.0,), waiting_probe_seconds=60.0),
        jitter=lambda: 0.0,
        event_sink=Sink(),
    )

    results: list[TaskResult] = []
    thread = Thread(
        target=lambda: results.append(runner.run_task(repository.task.id, CancellationToken())),
        daemon=True,
    )
    thread.start()
    assert retry_published.wait(2)

    started = monotonic()
    assert runner.shutdown(2.0) is True
    elapsed = monotonic() - started
    thread.join(2)

    assert not thread.is_alive()
    assert elapsed < 1.0
    assert results[0].state is TaskState.PAUSED


def test_runner_never_persists_password_outside_the_credentials_table(tmp_path: Path) -> None:
    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    task = replace(build_task_record(), state=TaskState.QUEUED)
    repository.create_task(task, [])
    events: list[object] = []
    seen: list[str] = []

    class Sink:
        def publish(self, event: object) -> None:
            events.append(event)

    def engine_factory(factory_task, password):
        del factory_task
        seen.append(password)
        raise PermissionError("denied")

    runner = RetryingTaskRunner(
        repository,
        Credentials(),
        engine_factory,
        sleeper=lambda _delay: None,
        jitter=lambda: 0.0,
        event_sink=Sink(),
    )

    result = runner.run_task(task.id, CancellationToken())

    # The password must reach the factory (positive control) but no durable
    # snapshot and no event may carry it.
    assert result.state is TaskState.FAILED
    assert seen == ["memory-only-secret"]
    assert "memory-only-secret" not in repr(events)

    connection = sqlite3.connect(tmp_path / "nasmove.db")
    try:
        # The password may only ever live in the ``credentials`` table, so scan
        # every other table rather than a hand-picked subset: a future table or
        # column must not be able to reintroduce a leak unnoticed.
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name <> 'credentials'"
            )
        ]
        assert "tasks" in table_names
        assert "transfer_items" in table_names
        assert "events" in table_names
        for table in table_names:
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
            assert "memory-only-secret" not in repr(rows), table
        assert connection.execute("SELECT count(*) FROM credentials").fetchone()[0] == 0

        # The invariant rests on the password never entering the domain object.
        assert "password" not in ConnectionConfig.__dataclass_fields__

        # The database is the only permitted landing point, and only inside the
        # credentials table, when the user explicitly saves the password.
        store = SqliteCredentialStore(repository)
        profile_id = task.connection.profile_id
        store.set_password(profile_id, "memory-only-secret")
        assert store.get_password(profile_id) == "memory-only-secret"
        assert connection.execute(
            "SELECT password FROM credentials WHERE profile_id = ?",
            (str(profile_id),),
        ).fetchone() == ("memory-only-secret",)
        store.delete_password(profile_id)
        assert store.get_password(profile_id) is None
    finally:
        connection.close()
    repository.close()


def test_engine_waits_for_network_when_worker_factory_fails_retryably() -> None:
    engine, repository, task, events = _engine_with_failing_factory(
        ConnectionResetError("NAS restarted")
    )

    result = engine.run_task(task.id, CancellationToken())

    assert result.state is TaskState.WAITING_FOR_NETWORK
    assert repository.get_task(task.id).state is TaskState.WAITING_FOR_NETWORK
    assert any(
        event.kind == "failed" and isinstance(event.error, ConnectionResetError)
        for event in events
    )


def test_engine_fails_task_when_worker_factory_fails_non_retryably() -> None:
    engine, repository, task, events = _engine_with_failing_factory(
        PermissionError("denied")
    )

    result = engine.run_task(task.id, CancellationToken())

    assert result.state is TaskState.FAILED
    assert repository.get_task(task.id).state is TaskState.FAILED
    assert any(
        event.kind == "failed" and isinstance(event.error, PermissionError)
        for event in events
    )


def _policy(*, delays: tuple[float, ...], waiting_probe_seconds: float = 60.0):
    from nasmove.core.retry import RetryPolicy

    return RetryPolicy(delays=delays, waiting_probe_seconds=waiting_probe_seconds)


def _engine_with_failing_factory(error: BaseException):
    trace: list[str] = []
    events: list[object] = []
    repository = TransferRepository(trace)
    task = replace(
        build_task_record(
            connection=build_connection_config(max_parallel_items=1)
        ),
        state=TaskState.QUEUED,
    )
    repository.tasks[task.id] = task
    item = build_transfer_item_record()
    repository.items[item.id] = item

    class Sink:
        def publish(self, event: object) -> None:
            events.append(event)

    def factory(factory_task, password):
        del factory_task, password
        raise error

    engine = TransferEngine(
        repository,
        item_worker_factory=factory,
        password="memory-only-secret",
        event_sink=Sink(),
    )
    return engine, repository, task, events
