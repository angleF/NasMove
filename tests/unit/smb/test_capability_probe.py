from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from smbprotocol.exceptions import SMBOSError
from smbprotocol.header import NtStatus

import nasmove.smb.capability_probe as capability_probe_module
from nasmove.core.model import ConnectionConfig, ConnectionProfileId, RemotePath
from nasmove.core.ports import RemoteStat
from nasmove.smb.capability_probe import SmbCapabilityProbe
from nasmove.smb.error_mapping import (
    RenameOutcomeUnknownError,
    StaleSmbHandleError,
    TargetExistsError,
    redacted_error_code,
)
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


def _fixed_probe_uuid() -> UUID:
    return UUID("12345678-1234-5678-1234-567812345678")


def test_rename_collision_never_deletes_preexisting_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = RecordingSmbGateway()
    renamed = "archive/incoming/.nasmove-probe-12345678123456781234567812345678.renamed"
    original = "archive/incoming/.nasmove-probe-12345678123456781234567812345678.tmp"
    protected_content = b"preexisting-owner-data"
    gateway.files[renamed] = protected_content
    monkeypatch.setattr(capability_probe_module, "uuid4", _fixed_probe_uuid)

    report = SmbCapabilityProbe(gateway).run(RemotePath("archive/incoming"))

    assert gateway.files[renamed] == protected_content
    assert original not in gateway.files
    assert report.rename_exclusive is False
    assert report.cleanup is False
    assert report.error_code == "rename_exclusive:target_exists"


def test_uncertain_rename_result_preserves_possible_destination_for_manual_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = RecordingSmbGateway(ambiguous_rename=True)
    renamed = "archive/incoming/.nasmove-probe-12345678123456781234567812345678.renamed"
    monkeypatch.setattr(capability_probe_module, "uuid4", _fixed_probe_uuid)

    report = SmbCapabilityProbe(gateway).run(RemotePath("archive/incoming"))

    assert renamed in gateway.files
    assert report.rename_exclusive is False
    assert report.cleanup is False
    assert report.error_code == "rename_exclusive:rename_outcome_unknown"


def test_uncertain_rename_cleanup_accepts_real_smb_missing_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = RecordingSmbGateway(ambiguous_rename=True, real_missing_remove=True)
    renamed = "archive/incoming/.nasmove-probe-12345678123456781234567812345678.renamed"
    monkeypatch.setattr(capability_probe_module, "uuid4", _fixed_probe_uuid)

    report = SmbCapabilityProbe(gateway).run(RemotePath("archive/incoming"))

    assert renamed in gateway.files
    assert report.cleanup is False
    assert report.error_code == "rename_exclusive:rename_outcome_unknown"


@pytest.mark.parametrize(
    ("ntstatus", "expected"),
    [
        (NtStatus.STATUS_ACCESS_DENIED, "permission_denied"),
        (NtStatus.STATUS_DISK_FULL, "disk_full"),
        (NtStatus.STATUS_OBJECT_NAME_COLLISION, "target_exists"),
    ],
)
def test_real_smb_os_error_ntstatus_maps_to_safe_code(ntstatus: int, expected: str) -> None:
    error = SMBOSError(ntstatus, r"\\private-nas\secret-share\customer-name")

    code = redacted_error_code(error)

    assert code == expected
    assert "private-nas" not in code
    assert "customer-name" not in code


def test_real_smb_exception_body_never_enters_capability_report() -> None:
    gateway = RecordingSmbGateway(
        create_error=SMBOSError(
            NtStatus.STATUS_ACCESS_DENIED,
            r"\\private-nas\secret-share\customer-name",
        )
    )

    report = SmbCapabilityProbe(gateway).run(RemotePath("archive/incoming"))

    assert report.error_code == "create:permission_denied"
    assert "private-nas" not in report.error_code
    assert "customer-name" not in report.error_code


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


@pytest.mark.parametrize(
    "ntstatus",
    [NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, NtStatus.STATUS_OBJECT_PATH_NOT_FOUND],
)
def test_stat_returns_none_for_real_smb_missing_status(
    monkeypatch: pytest.MonkeyPatch,
    ntstatus: int,
) -> None:
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )

    def missing_stat(path: str, **kwargs: Any) -> Any:
        raise SMBOSError(ntstatus, path)

    monkeypatch.setattr("nasmove.smb.smbprotocol_gateway.smbclient.stat", missing_stat)
    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "memory-only-secret")

    assert gateway.stat(RemotePath("archive/missing")) is None


def test_stat_reraises_real_smb_non_missing_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = SMBOSError(NtStatus.STATUS_ACCESS_DENIED, r"\\private\share\denied")
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )

    def denied_stat(path: str, **kwargs: Any) -> Any:
        del path, kwargs
        raise error

    monkeypatch.setattr("nasmove.smb.smbprotocol_gateway.smbclient.stat", denied_stat)
    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "memory-only-secret")

    with pytest.raises(SMBOSError) as captured:
        gateway.stat(RemotePath("archive/denied"))

    assert captured.value is error


def test_rename_exclusive_calls_smb_rename_after_real_missing_target_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rename_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )

    def missing_stat(path: str, **kwargs: Any) -> Any:
        raise SMBOSError(NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, path)

    def record_rename(source: str, target: str, **kwargs: Any) -> None:
        del kwargs
        rename_calls.append((source, target))

    monkeypatch.setattr("nasmove.smb.smbprotocol_gateway.smbclient.stat", missing_stat)
    monkeypatch.setattr("nasmove.smb.smbprotocol_gateway.smbclient.rename", record_rename)
    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "memory-only-secret")

    gateway.rename_exclusive(RemotePath("archive/source"), RemotePath("archive/target"))

    assert rename_calls == [
        (
            r"\\nas.example.test\test-share\archive\source",
            r"\\nas.example.test\test-share\archive\target",
        )
    ]


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


def test_rename_exclusive_marks_lost_response_as_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )

    def lose_rename_response(source: str, target: str, **kwargs: Any) -> None:
        del source, target, kwargs
        raise TimeoutError("private server response was lost")

    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.rename",
        lose_rename_response,
    )
    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "memory-only-secret")
    monkeypatch.setattr(gateway, "stat", lambda path: None)

    with pytest.raises(RenameOutcomeUnknownError):
        gateway.rename_exclusive(RemotePath("a/source"), RemotePath("a/target"))


def test_replace_atomic_calls_smb_replace_and_redacts_unc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.replace",
        lambda source, target, **kwargs: calls.append((source, target)),
    )
    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "memory-only-secret")

    gateway.replace_atomic(RemotePath("a/source"), RemotePath("a/target"))

    assert calls == [
        (
            r"\\nas.example.test\test-share\a\source",
            r"\\nas.example.test\test-share\a\target",
        )
    ]


def test_replace_atomic_marks_lost_response_as_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("lost")),
    )
    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "memory-only-secret")

    with pytest.raises(RenameOutcomeUnknownError):
        gateway.replace_atomic(RemotePath("a/source"), RemotePath("a/target"))
