from __future__ import annotations

import stat
from pathlib import Path

from nasmove.core.model import ConnectionConfig, ConnectionProfileId
from nasmove.ui.desktop_app import ProductionConnectionTester, build_desktop_runtime
from nasmove.ui.view_models import ConnectionRequest


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
