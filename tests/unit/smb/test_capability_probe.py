from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nasmove.core.model import ConnectionConfig, ConnectionProfileId, RemotePath
from nasmove.core.ports import RemoteStat
from nasmove.smb.capability_probe import SmbCapabilityProbe
from nasmove.smb.error_mapping import StaleSmbHandleError, TargetExistsError
from nasmove.smb.smbprotocol_gateway import SmbProtocolGateway
from tests.fixtures.fake_smb import RecordingSmbGateway


def test_probe_requires_random_write_flush_truncate_rename_and_cleanup() -> None:
    gateway = RecordingSmbGateway()
    report = SmbCapabilityProbe(gateway).run(RemotePath("archive/incoming"))
    assert report.random_write is True
    assert report.flush is True
    assert report.truncate is True
    assert report.rename_exclusive is True
    assert report.cleanup is True
    assert gateway.calls == [
        "create_exclusive",
        "write_initial",
        "flush",
        "reopen_update",
        "seek_append",
        "truncate",
        "rename_exclusive",
        "open_read",
        "remove_file",
    ]


def test_probe_uses_unique_small_files_and_leaves_no_residue() -> None:
    gateway = RecordingSmbGateway()

    first = SmbCapabilityProbe(gateway).run(RemotePath("archive/incoming"))
    second = SmbCapabilityProbe(gateway).run(RemotePath("archive/incoming"))

    assert first.all_supported is True
    assert second.all_supported is True
    assert gateway.created_paths[0] != gateway.created_paths[1]
    assert all(path.value.split("/")[-1].startswith(".nasmove-probe-") for path in gateway.created_paths)
    assert gateway.max_file_size <= 1024 * 1024
    assert gateway.files == {}


def test_cleanup_failure_is_explicitly_reported_without_sensitive_message() -> None:
    gateway = RecordingSmbGateway(fail_cleanup=True)

    report = SmbCapabilityProbe(gateway).run(RemotePath("private/customer-name"))

    assert report.cleanup is False
    assert report.all_supported is False
    assert report.error_code == "cleanup:permission_denied"
    assert "customer-name" not in report.error_code


def test_full_read_must_match_expected_truncated_content() -> None:
    gateway = RecordingSmbGateway(corrupt_read=True)

    report = SmbCapabilityProbe(gateway).run(RemotePath("archive/incoming"))

    assert report.random_write is False
    assert report.truncate is False
    assert report.rename_exclusive is True
    assert report.cleanup is True
    assert report.error_code == "verify:content_mismatch"


def _config() -> ConnectionConfig:
    return ConnectionConfig(
        profile_id=ConnectionProfileId("probe"),
        display_name="Probe",
        host="nas.example.test",
        share="test-share",
        username="tester",
        domain="LAB",
        require_encryption=True,
    )


def _session(*, dialect: int = 0x0311) -> SimpleNamespace:
    connection = SimpleNamespace(dialect=dialect)
    return SimpleNamespace(
        connection=connection,
        signing_required=True,
        encrypt_data=True,
    )


def test_connect_returns_negotiated_security_and_increments_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def register_session(*args: Any, **kwargs: Any) -> SimpleNamespace:
        register_calls.append((args, kwargs))
        return _session()

    monkeypatch.setattr("nasmove.smb.smbprotocol_gateway.smbclient.register_session", register_session)
    gateway = SmbProtocolGateway()

    first = gateway.connect(_config(), "memory-only-secret")
    second = gateway.connect(_config(), "memory-only-secret")

    assert first.dialect == "3.1.1"
    assert first.signing is True
    assert first.encryption is True
    assert first.session_generation == 1
    assert second.session_generation == 2
    assert register_calls[0][0] == ("nas.example.test",)
    assert register_calls[0][1]["username"] == "LAB\\tester"
    assert register_calls[0][1]["encrypt"] is True


def test_open_update_uses_normalized_unc_binary_update_and_reset_invalidates_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: dict[str, Any] = {}
    resets: list[bool] = []

    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )

    def open_file(path: str, **kwargs: Any) -> Any:
        from io import BytesIO

        opened.update(path=path, **kwargs)
        return BytesIO(b"known")

    monkeypatch.setattr("nasmove.smb.smbprotocol_gateway.smbclient.open_file", open_file)
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.reset_connection_cache",
        lambda **kwargs: resets.append(True),
    )
    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "memory-only-secret")

    with gateway.open_update(RemotePath("archive/file.part")) as stream:
        assert stream.read() == b"known"
        gateway.reset_connection()
        with pytest.raises(StaleSmbHandleError):
            stream.seek(0)

    assert opened["path"] == r"\\nas.example.test\test-share\archive\file.part"
    assert opened["mode"] == "r+b"
    assert opened["buffering"] == 0
    assert resets == [True]


def test_rename_exclusive_refuses_existing_target_without_calling_smb_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rename_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.rename",
        lambda source, target, **kwargs: rename_calls.append((source, target)),
    )
    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "memory-only-secret")
    monkeypatch.setattr(
        gateway,
        "stat",
        lambda path: RemoteStat(size=1, is_directory=False, modified_ns=0, file_id="1"),
    )

    with pytest.raises(TargetExistsError):
        gateway.rename_exclusive(RemotePath("a/source"), RemotePath("a/target"))

    assert rename_calls == []
