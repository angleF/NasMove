from __future__ import annotations

from dataclasses import dataclass
from threading import Event, get_ident
from typing import Any

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from nasmove.ui.connection_page import ConnectionPage
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage
from nasmove.ui.view_models import ConnectionTestReport
from tests.fixtures.fake_smb import RecordingSmbGateway

_STAGE_NAMES = ("地址解析", "TCP", "SMB 协商", "认证", "共享访问")


class FakeConnectionService:
    """Records the calling thread ID so tests can verify UI-thread isolation."""

    def __init__(self) -> None:
        self.called = False
        self.thread_id: int | None = None
        self.requests: list[Any] = []
        self.report = ConnectionTestReport.all_passed()

    def test_connection(self, request: Any) -> ConnectionTestReport:
        self.called = True
        self.thread_id = get_ident()
        self.requests.append(request)
        return self.report


class FakeCredentialStore:
    """In-memory credential store; passwords never leave process memory."""

    def __init__(self) -> None:
        self.passwords: dict[str, str] = {}
        self.set_calls: list[str] = []
        self.delete_calls: list[str] = []

    def get_password(self, profile_id: object) -> str | None:
        return self.passwords.get(str(profile_id))

    def set_password(self, profile_id: object, password: str) -> None:
        self.set_calls.append(str(profile_id))
        self.passwords[str(profile_id)] = password

    def delete_password(self, profile_id: object) -> None:
        self.delete_calls.append(str(profile_id))
        self.passwords.pop(str(profile_id), None)


class ThreadRecordingSmbGateway:
    """Wraps a recording gateway and records the thread ID of SMB calls."""

    LIST_DIR_TIMEOUT_SECONDS = 5.0

    def __init__(self, inner: RecordingSmbGateway) -> None:
        self.inner = inner
        self.thread_ids: list[int] = []
        self.list_dir_calls: list[object] = []
        self.list_dir_gate: Event | None = None

    def list_dir(self, path: object) -> list[Any]:
        self.list_dir_calls.append(path)
        self.thread_ids.append(int(QThread.currentThreadId()))
        if self.list_dir_gate is not None:
            self.list_dir_gate.wait(self.LIST_DIR_TIMEOUT_SECONDS)
        return self.inner.list_dir(path)  # type: ignore[arg-type]

    def list_share_root(self) -> list[Any]:
        self.thread_ids.append(int(QThread.currentThreadId()))
        return self.inner.list_share_root()

    def free_space(self, path: object) -> int:
        self.thread_ids.append(int(QThread.currentThreadId()))
        return self.inner.free_space(path)  # type: ignore[arg-type]

    def free_space_share_root(self) -> int:
        self.thread_ids.append(int(QThread.currentThreadId()))
        return self.inner.free_space_share_root()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@dataclass
class UiFixture:
    qt_application: QApplication
    connection_page: ConnectionPage
    source_page: SourcePage
    target_page: TargetPage
    fake_service: FakeConnectionService
    credential_store: FakeCredentialStore
    gateway: ThreadRecordingSmbGateway
    ui_thread_id: int


@pytest.fixture
def ui_fixture(qtbot: Any, qapp: QApplication) -> UiFixture:
    fake_service = FakeConnectionService()
    credential_store = FakeCredentialStore()
    gateway = ThreadRecordingSmbGateway(RecordingSmbGateway())
    gateway.inner.files["docs/readme.txt"] = b"hello"
    connection_page = ConnectionPage(tester=fake_service, credential_store=credential_store)
    source_page = SourcePage()
    target_page = TargetPage(gateway=gateway)
    for page in (connection_page, source_page, target_page):
        qtbot.addWidget(page)
    return UiFixture(
        qt_application=qapp,
        connection_page=connection_page,
        source_page=source_page,
        target_page=target_page,
        fake_service=fake_service,
        credential_store=credential_store,
        gateway=gateway,
        ui_thread_id=get_ident(),
    )
