from __future__ import annotations

import sqlite3
from dataclasses import replace

from nasmove.core.model import ConnectionProfileId
from nasmove.core.states import TaskState
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from nasmove.security.sqlite_store import SqliteCredentialStore
from nasmove.ui.connection_profile_service import ConnectionProfileService
from tests.fixtures.builders import build_connection_config, build_task_record


def _repository(tmp_path) -> SqliteTaskRepository:
    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    task = replace(build_task_record(), state=TaskState.QUEUED)
    repository.create_task(task, [])
    return repository


def test_store_round_trip_write_read_delete(tmp_path) -> None:
    repository = _repository(tmp_path)
    store = SqliteCredentialStore(repository)
    profile_id = ConnectionProfileId("connection-profile-1")

    assert store.get_password(profile_id) is None
    store.set_password(profile_id, "密码🔐")
    assert store.get_password(profile_id) == "密码🔐"
    store.delete_password(profile_id)
    assert store.get_password(profile_id) is None
    repository.close()


def test_store_overwrites_existing_password(tmp_path) -> None:
    repository = _repository(tmp_path)
    store = SqliteCredentialStore(repository)
    profile_id = ConnectionProfileId("connection-profile-1")

    store.set_password(profile_id, "first")
    store.set_password(profile_id, "second")

    assert store.get_password(profile_id) == "second"
    connection = sqlite3.connect(tmp_path / "nasmove.db")
    rows = connection.execute(
        "SELECT password FROM credentials WHERE profile_id = ?", (str(profile_id),)
    ).fetchall()
    connection.close()
    assert rows == [("second",)]
    repository.close()


def test_store_returns_none_for_unknown_profile(tmp_path) -> None:
    repository = _repository(tmp_path)
    store = SqliteCredentialStore(repository)

    assert store.get_password(ConnectionProfileId("absent")) is None
    repository.close()


def test_delete_is_idempotent_for_missing_profile(tmp_path) -> None:
    repository = _repository(tmp_path)
    store = SqliteCredentialStore(repository)

    store.delete_password(ConnectionProfileId("absent"))
    store.delete_password(ConnectionProfileId("connection-profile-1"))
    repository.close()


def test_archiving_profile_removes_its_credential(tmp_path) -> None:
    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    config = build_connection_config()
    repository.save_successful_connection(config)
    store = SqliteCredentialStore(repository)
    store.set_password(config.profile_id, "secret")

    result = ConnectionProfileService(repository, store).archive(config.profile_id)

    assert result.archived is True
    assert result.credential_removed is True
    assert store.get_password(config.profile_id) is None
    repository.close()


def test_startup_without_stored_credential_pauses_queued_task(tmp_path) -> None:
    from nasmove.app import ApplicationService

    class _Queue:
        def __init__(self) -> None:
            self.enqueued: list[object] = []

        def enqueue(self, task_id: object) -> None:
            self.enqueued.append(task_id)

    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    config = build_connection_config()
    repository.save_successful_connection(config)
    task = replace(build_task_record(), state=TaskState.QUEUED)
    repository.create_task(task, [])
    store = SqliteCredentialStore(repository)
    queue = _Queue()

    service = ApplicationService(
        repository=repository,
        queue=queue,
        credential_store=store,
        lock_path=tmp_path / "app.lock",
    )
    report = service.start()

    assert report.paused_tasks == 1
    assert repository.get_task(task.id).state is TaskState.PAUSED
    assert store.get_password(task.connection.profile_id) is None
    assert queue.enqueued == []
    repository.close()
