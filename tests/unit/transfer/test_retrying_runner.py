from dataclasses import replace

from nasmove.core.ports import SessionInfo
from nasmove.core.retry import RetryPolicy
from nasmove.core.states import TaskState
from nasmove.transfer.checkpoint_writer import CancellationToken
from nasmove.transfer.retrying_runner import RetryingTaskRunner
from nasmove.transfer.transfer_engine import TaskResult
from tests.fixtures.builders import build_task_record


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


class Gateway:
    def __init__(self, failures=()) -> None:
        self.failures = list(failures)
        self.connect_calls = 0
        self.reset_calls = 0

    def connect(self, config, password):
        del config
        assert password == "memory-only-secret"
        self.connect_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return SessionInfo("3.1.1", True, True, self.connect_calls)

    def reset_connection(self) -> None:
        self.reset_calls += 1


def test_runner_reconnects_and_resumes_waiting_task() -> None:
    repository = Repository()
    gateway = Gateway()
    results = [
        TaskResult(False, TaskState.WAITING_FOR_NETWORK, error=ConnectionResetError()),
        TaskResult(True, TaskState.COMPLETED),
    ]
    sleeps: list[float] = []

    def engine_factory(session):
        class Engine:
            def run_task(self, task_id, token):
                del task_id, token
                assert session.session_generation > 0
                result = results.pop(0)
                repository.task = replace(repository.task, state=result.state)
                return result

        return Engine()

    runner = RetryingTaskRunner(
        repository,
        gateway,
        Credentials(),
        engine_factory,
        retry_policy=RetryPolicy(delays=(1.0,)),
        sleeper=sleeps.append,
        jitter=lambda: 0.0,
    )

    result = runner.run_task(repository.task.id, CancellationToken())

    assert result.success is True
    assert gateway.connect_calls == 2
    assert gateway.reset_calls == 1
    assert sleeps == [1.0]


def test_runner_waits_at_probe_interval_after_fast_retries() -> None:
    repository = Repository()
    gateway = Gateway()
    attempts = 0
    sleeps: list[float] = []

    def engine_factory(session):
        del session

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
        gateway,
        Credentials(),
        engine_factory,
        retry_policy=RetryPolicy(delays=(1.0, 2.0), waiting_probe_seconds=60.0),
        sleeper=sleeps.append,
        jitter=lambda: 0.0,
    )

    result = runner.run_task(repository.task.id, CancellationToken())

    assert result.success is True
    assert sleeps == [1.0, 2.0, 60.0]


def test_runner_stops_retrying_non_network_connection_failure() -> None:
    repository = Repository()
    gateway = Gateway(failures=(PermissionError("denied"),))
    runner = RetryingTaskRunner(
        repository,
        gateway,
        Credentials(),
        lambda session: None,
        sleeper=lambda delay: None,
        jitter=lambda: 0.0,
    )

    result = runner.run_task(repository.task.id, CancellationToken())

    assert result.success is False
    assert result.state is TaskState.FAILED
    assert repository.task.state is TaskState.FAILED
    assert gateway.connect_calls == 1
