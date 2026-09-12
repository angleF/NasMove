from __future__ import annotations

import stat
import time
from dataclasses import replace
from itertools import count
from pathlib import Path
from threading import Barrier, Thread, get_ident
from typing import Any, cast

import pytest

from nasmove.core.model import (
    ConnectionConfig,
    ConnectionProfileId,
    TaskRecord,
    TransferItemId,
)
from nasmove.core.ports import SessionInfo
from nasmove.core.states import TaskState
from nasmove.localio.files import PosixLocalFileGateway
from nasmove.transfer.checkpoint_writer import CancellationToken
from nasmove.transfer.progress import ProgressSnapshot, ProgressTracker
from nasmove.ui.desktop_app import (
    ProductionConnectionTester,
    build_desktop_runtime,
    build_engine,
)
from nasmove.ui.view_models import ConnectionRequest
from tests.fixtures.builders import (
    build_connection_config,
    build_task_record,
    build_transfer_item_record,
)
from tests.fixtures.transfer import TransferRepository


class _Credentials:
    def get_password(self, _profile_id: object) -> None:
        return None

    def set_password(self, _profile_id: object, _password: str) -> None:
        return

    def delete_password(self, _profile_id: object) -> None:
        return


class _Gateway:
    def __init__(self) -> None:
        self.connected = False
        self.share_probed = False

    def connect(self, _config: object, _password: str) -> None:
        self.connected = True

    def probe_share(self) -> None:
        self.share_probed = True

    def reset_connection(self) -> None:
        return


def _request() -> ConnectionRequest:
    return ConnectionRequest(
        ConnectionConfig(
            profile_id=ConnectionProfileId("profile"),
            display_name="NAS",
            host="nas.local",
            share="archive",
            username="operator",
        ),
        "secret",
    )


def test_production_connection_tester_checks_tcp_smb_auth_and_share() -> None:
    gateway = _Gateway()
    tester = ProductionConnectionTester(
        gateway,
        tcp_probe=lambda _address, _timeout: None,
    )

    report = tester.test_connection(_request())

    assert report.success is True
    assert gateway.connected is True
    assert gateway.share_probed is True


def test_build_desktop_runtime_wires_real_components(
    qtbot,
    tmp_path: Path,
) -> None:
    runtime = build_desktop_runtime(
        data_dir=tmp_path,
        credential_store=_Credentials(),
    )
    qtbot.addWidget(runtime.window)

    report = runtime.application.start()

    assert report.started is True
    assert runtime.window.task_creation_controller is not None
    assert (tmp_path / "nasmove.db").exists()
    assert runtime.application.request_shutdown().completed is True


def test_build_desktop_runtime_hardens_owned_existing_data_directory(
    qtbot,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "NasMove"
    data_dir.mkdir(mode=0o755)

    runtime = build_desktop_runtime(
        data_dir=data_dir,
        credential_store=_Credentials(),
    )
    qtbot.addWidget(runtime.window)

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    runtime.repository.close()


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.snapshots: list[ProgressSnapshot] = []
        self.target = self

    def publish(self, event: object) -> None:
        self.events.append(event)

    def publish_progress(self, snapshot: ProgressSnapshot) -> None:
        self.snapshots.append(snapshot)


class _WorkerRepository:
    def __init__(self) -> None:
        self.released: list[int] = []

    def list_items(self, _task_id: object) -> list[object]:
        return []

    def release_thread_connection(self) -> None:
        self.released.append(get_ident())


class _RecordingGateway:
    def __init__(self, session: SessionInfo) -> None:
        self.session = session
        self.connect_calls: list[tuple[object, str]] = []
        self.disconnect_calls = 0

    def connect(self, config: object, password: str) -> SessionInfo:
        self.connect_calls.append((config, password))
        return self.session

    def disconnect(self) -> None:
        self.disconnect_calls += 1


class _FailingGateway:
    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.disconnect_calls = 0

    def connect(self, _config: object, _password: str) -> None:
        raise self._error

    def disconnect(self) -> None:
        self.disconnect_calls += 1


def _build_engine(
    task: TaskRecord,
    *,
    repository: _WorkerRepository,
    events: _RecordingSink,
    created: list[_RecordingGateway],
    counter: count,
) -> object:
    def gateway_factory() -> _RecordingGateway:
        gateway = _RecordingGateway(SessionInfo("3.1.1", True, True, next(counter)))
        created.append(gateway)
        return gateway

    return build_engine(
        task,
        "memory-only-secret",
        repository=cast(Any, repository),
        local=PosixLocalFileGateway(),
        events=events,
        gateway_factory=cast(Any, gateway_factory),
    )


def test_task_engine_gives_each_worker_an_isolated_gateway_and_thread_connection() -> None:
    task = replace(build_task_record(), state=TaskState.RUNNING)
    repository = _WorkerRepository()
    events = _RecordingSink()
    created: list[_RecordingGateway] = []
    engine = cast(Any, _build_engine(
        task,
        repository=repository,
        events=events,
        created=created,
        counter=count(1),
    ))
    factory = engine._item_worker_factory

    barrier = Barrier(2)

    def build_and_close() -> None:
        barrier.wait(5)
        worker = factory(task, "memory-only-secret")
        worker.close()

    threads = [Thread(target=build_and_close) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not any(thread.is_alive() for thread in threads)
    assert len(created) == 2
    assert created[0] is not created[1]
    assert len({id(gateway) for gateway in created}) == 2
    assert [call[1] for gateway in created for call in gateway.connect_calls] == [
        "memory-only-secret",
        "memory-only-secret",
    ]
    assert created[0].session is not created[1].session
    assert sorted(
        gateway.session.session_generation for gateway in created
    ) == [1, 2]
    assert len(set(repository.released)) == 2
    assert all(gateway.disconnect_calls == 1 for gateway in created)


def test_task_engine_offset_maps_survive_concurrent_copy_and_verify_updates() -> None:
    task = replace(build_task_record(), state=TaskState.RUNNING)
    repository = _WorkerRepository()
    events = _RecordingSink()
    engine = cast(Any, _build_engine(
        task,
        repository=repository,
        events=events,
        created=[],
        counter=count(1),
    ))
    worker = engine._item_worker_factory(task, "memory-only-secret")
    copy_progress = worker._writer._progress
    verify_progress = worker._verifier._progress
    worker.close()

    items = [
        replace(build_transfer_item_record(), id=TransferItemId(f"item-{index}"))
        for index in range(1, 7)
    ]
    final_offset = 500

    def hammer(callback, item) -> None:
        for offset in range(1, final_offset + 1):
            callback(item, offset)

    threads = [
        Thread(target=hammer, args=(callback, item))
        for item in items
        for callback in (copy_progress, verify_progress)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not any(thread.is_alive() for thread in threads)
    # Let the tracker's notification throttle elapse so the settling update
    # below always publishes, then read the totals it observed.
    time.sleep(ProgressTracker.EVENT_INTERVAL_SECONDS + 0.05)
    copy_progress(items[0], final_offset)
    verify_progress(items[0], final_offset)
    expected = len(items) * final_offset
    snapshot = events.snapshots[-1]
    assert snapshot.copied_bytes == expected
    assert snapshot.verified_bytes == expected


def test_task_engine_worker_has_no_second_progress_channel() -> None:
    task = replace(build_task_record(), state=TaskState.RUNNING)
    events = _RecordingSink()
    engine = cast(Any, _build_engine(
        task,
        repository=_WorkerRepository(),
        events=events,
        created=[],
        counter=count(1),
    ))
    worker = engine._item_worker_factory(task, "memory-only-secret")
    item = replace(build_transfer_item_record(), id=TransferItemId("item-1"))

    # The absolute offset-map callbacks are the single progress source.  A live
    # progress_tracker would add an increment on top of the absolute update and
    # inflate the reported total, so the worker must be built without one.
    assert worker._progress is None

    worker._record_progress("record_copy", 100)
    copy_progress = worker._writer._progress
    copy_progress(item, 250)
    worker.close()

    assert events.snapshots[-1].copied_bytes == 250


def test_task_engine_releases_worker_resources_when_the_gateway_connect_fails() -> None:
    task = replace(
        build_task_record(connection=build_connection_config(max_parallel_items=1)),
        state=TaskState.RUNNING,
    )
    repository = _WorkerRepository()
    created: list[_FailingGateway] = []

    def gateway_factory() -> _FailingGateway:
        gateway = _FailingGateway(ConnectionResetError("NAS is down"))
        created.append(gateway)
        return gateway

    engine = cast(Any, build_engine(
        task,
        "memory-only-secret",
        repository=cast(Any, repository),
        local=PosixLocalFileGateway(),
        events=_RecordingSink(),
        gateway_factory=cast(Any, gateway_factory),
    ))

    with pytest.raises(ConnectionResetError):
        engine._item_worker_factory(task, "memory-only-secret")

    assert [gateway.disconnect_calls for gateway in created] == [1]
    assert repository.released == [get_ident()]


@pytest.mark.parametrize(
    ("error", "expected_state"),
    [
        (ConnectionResetError("NAS is down"), TaskState.WAITING_FOR_NETWORK),
        (PermissionError("denied"), TaskState.FAILED),
    ],
)
def test_task_engine_classifies_a_failed_worker_build(
    error: BaseException,
    expected_state: TaskState,
) -> None:
    repository = TransferRepository([])
    task = replace(
        build_task_record(connection=build_connection_config(max_parallel_items=1)),
        state=TaskState.QUEUED,
    )
    item = build_transfer_item_record()
    repository.tasks[task.id] = task
    repository.items[item.id] = item
    created: list[_FailingGateway] = []

    def gateway_factory() -> _FailingGateway:
        gateway = _FailingGateway(error)
        created.append(gateway)
        return gateway

    engine = cast(Any, build_engine(
        task,
        "memory-only-secret",
        repository=cast(Any, repository),
        local=PosixLocalFileGateway(),
        events=_RecordingSink(),
        gateway_factory=cast(Any, gateway_factory),
    ))

    result = engine.run_task(task.id, CancellationToken())

    assert result.state is expected_state
    assert repository.get_task(task.id).state is expected_state
    assert [gateway.disconnect_calls for gateway in created] == [1]
    assert repository.release_calls == 1


def test_application_shutdown_keeps_resources_open_until_safe_boundary(app_fixture) -> None:
    # Guards the production close ordering.  While the queue reports it has not
    # reached a safe boundary, shutdown must return without flushing, closing
    # the SMB session or closing the repository: a live worker still holds a
    # SQLite thread connection that close() would tear down underneath it.
    app_fixture.service.start()

    timed_out = app_fixture.service.request_shutdown(timeout=0.001)

    assert timed_out.completed is False
    assert timed_out.timed_out is True
    assert timed_out.safe_pausing is True
    assert "flush_checkpoint" not in app_fixture.trace
    assert "disconnect_smb" not in app_fixture.trace
    assert "close_db" not in app_fixture.trace
    assert app_fixture.repository.closed is False
    assert app_fixture.smb.closed is False

    # Positive control: once the boundary is reached the same call closes.
    app_fixture.queue.boundary_reached.set()
    completed = app_fixture.service.request_shutdown(timeout=1)

    assert completed.completed is True
    assert app_fixture.repository.closed is True
    assert app_fixture.smb.closed is True
