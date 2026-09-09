from __future__ import annotations

import os
import socket
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from PySide6.QtWidgets import QApplication, QMessageBox

from nasmove.app import ApplicationService
from nasmove.core.ports import SessionInfo
from nasmove.localio.files import PosixLocalFileGateway
from nasmove.persistence.sqlite_repository import SqliteTaskRepository
from nasmove.planning.task_planner import TaskPlanner
from nasmove.security.keychain_store import MacOSKeychainCredentialStore
from nasmove.smb.error_mapping import redacted_error_code
from nasmove.smb.smbprotocol_gateway import SmbProtocolGateway
from nasmove.transfer.checkpoint_writer import CheckpointWriter
from nasmove.transfer.commit import TargetCommitter
from nasmove.transfer.deletion import SourceDeletionService
from nasmove.transfer.progress import ProgressTracker
from nasmove.transfer.recovery import RecoveryCoordinator
from nasmove.transfer.retrying_runner import RetryingTaskRunner
from nasmove.transfer.transfer_engine import QueueCoordinator, TransferEngine
from nasmove.transfer.verification import IntegrityVerifier
from nasmove.ui.main_window import MainWindow
from nasmove.ui.view_models import ConnectionStageResult, ConnectionTestReport


class ProductionConnectionTester:
    """Test TCP, SMB negotiation/authentication, and share tree access."""

    def __init__(
        self,
        gateway: object,
        *,
        tcp_probe: Callable[[tuple[str, int], float], object] = socket.create_connection,
    ) -> None:
        self._gateway = gateway
        self._tcp_probe = tcp_probe

    def test_connection(self, request: object) -> ConnectionTestReport:
        config = cast(Any, request).config
        password = cast(str, cast(Any, request).password)
        stages = [ConnectionStageResult("地址解析", True)]
        try:
            connection = self._tcp_probe((config.host, config.port), 5.0)
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        except Exception as error:  # noqa: BLE001
            code = redacted_error_code(error)
            return ConnectionTestReport(
                tuple(
                    stages
                    + [
                        ConnectionStageResult("TCP", False, code),
                        ConnectionStageResult("SMB 协商", False),
                        ConnectionStageResult("认证", False),
                        ConnectionStageResult("共享访问", False),
                    ]
                )
            )
        stages.append(ConnectionStageResult("TCP", True))
        try:
            cast(Any, self._gateway).connect(config, password)
        except Exception as error:  # noqa: BLE001
            code = redacted_error_code(error)
            cast(Any, self._gateway).reset_connection()
            negotiation_ok = code not in {"unsupported", "unsupported_dialect"}
            return ConnectionTestReport(
                tuple(
                    stages
                    + [
                        ConnectionStageResult("SMB 协商", negotiation_ok, None if negotiation_ok else code),
                        ConnectionStageResult("认证", False, code if negotiation_ok else None),
                        ConnectionStageResult("共享访问", False),
                    ]
                )
            )
        stages.extend(
            (ConnectionStageResult("SMB 协商", True), ConnectionStageResult("认证", True))
        )
        try:
            cast(Any, self._gateway).probe_share()
        except Exception as error:  # noqa: BLE001
            cast(Any, self._gateway).reset_connection()
            stages.append(
                ConnectionStageResult("共享访问", False, redacted_error_code(error))
            )
        else:
            stages.append(ConnectionStageResult("共享访问", True))
        return ConnectionTestReport(tuple(stages))


class _UiEventSink:
    def __init__(self) -> None:
        self.target: object | None = None

    def publish(self, event: object) -> None:
        publish = getattr(self.target, "publish", None)
        if callable(publish):
            publish(event)


@dataclass(slots=True)
class DesktopRuntime:
    window: MainWindow
    application: ApplicationService
    repository: SqliteTaskRepository
    gateway: SmbProtocolGateway
    queue: QueueCoordinator


def _prepare_data_directory(root: Path) -> None:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = os.lstat(root)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("application data path must be a real directory")
    if info.st_uid != os.getuid():
        raise PermissionError("application data directory must be owned by the current user")
    os.chmod(root, 0o700)
    hardened = os.lstat(root)
    if stat.S_IMODE(hardened.st_mode) != 0o700:
        raise PermissionError("application data directory could not be made private")


def build_desktop_runtime(
    *,
    data_dir: Path | None = None,
    credential_store: object | None = None,
) -> DesktopRuntime:
    root = data_dir or Path.home() / "Library" / "Application Support" / "NasMove"
    _prepare_data_directory(root)
    repository = SqliteTaskRepository(root / "nasmove.db")
    gateway = SmbProtocolGateway()
    credentials = credential_store or MacOSKeychainCredentialStore()
    local = PosixLocalFileGateway()
    events = _UiEventSink()

    def build_engine(session: SessionInfo) -> TransferEngine:
        protocol_repository = cast(Any, repository)
        active = next(
            (
                task
                for task in repository.list_incomplete_tasks()
                if task.state.value in {"running", "waiting_for_network"}
            ),
            None,
        )
        progress = ProgressTracker(
            0 if active is None else active.total_bytes,
            event_sink=lambda snapshot: getattr(events.target, "publish_progress", lambda _: None)(
                snapshot
            ),
        )
        copied: dict[object, int] = {}
        verified: dict[object, int] = {}
        if active is not None:
            for item in repository.list_items(active.id):
                if item.state.value in {"committed", "source_delete_authorized", "done", "source_retained"}:
                    copied[item.id] = item.source_fingerprint.size
                    verified[item.id] = item.source_fingerprint.size

        def copy_progress(item: Any, offset: int) -> None:
            copied[item.id] = offset
            progress.update(copied_bytes=sum(copied.values()), verified_bytes=sum(verified.values()))

        def verify_progress(item: Any, offset: int) -> None:
            verified[item.id] = offset
            progress.update(copied_bytes=sum(copied.values()), verified_bytes=sum(verified.values()))

        verifier = IntegrityVerifier(protocol_repository, local, gateway, session, progress=verify_progress)
        return TransferEngine(
            repository,
            recovery=RecoveryCoordinator(protocol_repository, local, gateway, session),
            checkpoint_writer=CheckpointWriter(repository, local, gateway, progress=copy_progress),
            verifier=verifier,
            committer=TargetCommitter(protocol_repository, gateway, session),
            deletion_service=SourceDeletionService(
                protocol_repository, local, gateway, verifier
            ),
            smb_gateway=gateway,
            session=session,
            event_sink=events,
        )

    runner = RetryingTaskRunner(repository, gateway, credentials, build_engine, event_sink=events)
    queue = QueueCoordinator(cast(TransferEngine, runner), repository)
    application = ApplicationService(
        repository=repository,
        queue=queue,
        smb_gateway=gateway,
        credential_store=credentials,
        lock_path=root / "app.lock",
        database_path=root / "nasmove.db",
        transfer_ownership=True,
    )
    window = MainWindow(
        gateway=gateway,
        tester=ProductionConnectionTester(gateway),
        credential_store=credentials,
        task_repository=repository,
        task_planner=TaskPlanner(local, gateway, repository),
        application=application,
        queue_coordinator=queue,
    )
    events.target = window.task_controller
    return DesktopRuntime(window, application, repository, gateway, queue)


def main() -> int:
    qt_application = QApplication.instance() or QApplication([])
    runtime = build_desktop_runtime()
    startup = runtime.application.start()
    if not startup.started:
        QMessageBox.warning(None, "NasMove", startup.error or "应用启动失败")
        return 2
    runtime.window.task_controller.load_queue(tuple(runtime.repository.list_tasks()))
    if runtime.window.queue_execution is not None:
        runtime.window.queue_execution.start()
    qt_application.aboutToQuit.connect(runtime.application.request_shutdown)
    runtime.window.resize(1100, 760)
    runtime.window.show()
    return int(qt_application.exec())


__all__ = [
    "DesktopRuntime",
    "ProductionConnectionTester",
    "build_desktop_runtime",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
