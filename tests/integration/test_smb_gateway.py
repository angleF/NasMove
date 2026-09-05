from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest

from nasmove.core.model import ConnectionConfig, ConnectionProfileId, RemotePath
from nasmove.smb.error_mapping import TargetExistsError
from nasmove.smb.smbprotocol_gateway import SmbProtocolGateway, write_all
from tests.fixtures.fake_smb import ShortWritingStream


def _config() -> ConnectionConfig:
    return ConnectionConfig(
        profile_id=ConnectionProfileId("gateway"),
        display_name="Gateway",
        host="nas.example.test",
        share="transfer",
        username="tester",
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        connection=SimpleNamespace(dialect=0x0311),
        signing_required=True,
        encrypt_data=True,
    )


def test_write_all_retries_short_writes() -> None:
    stream = ShortWritingStream(max_bytes_per_call=3)

    write_all(stream, b"abcdefgh")

    assert stream.value == b"abcdefgh"
    assert stream.write_calls == 3


def test_gateway_uses_normalized_unc_and_resets_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.reset_connection_cache",
        lambda **kwargs: calls.append(("reset", kwargs)),
    )

    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "secret")
    gateway.disconnect()
    gateway.disconnect()
    gateway.reset_connection()
    gateway.reset_connection()

    assert [name for name, _ in calls] == ["reset", "reset", "reset", "reset"]


def test_open_update_exposes_binary_context_and_short_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: dict[str, Any] = {}
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )

    def open_file(path: str, **kwargs: Any) -> BytesIO:
        opened.update(path=path, **kwargs)
        return BytesIO()

    monkeypatch.setattr("nasmove.smb.smbprotocol_gateway.smbclient.open_file", open_file)
    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "secret")

    with gateway.open_update(RemotePath("folder/file.part")) as stream:
        write_all(stream, b"payload")

    assert opened["path"] == r"\\nas.example.test\transfer\folder\file.part"
    assert opened["mode"] == "r+b"
    assert opened["buffering"] == 0


def test_rename_exclusive_checks_target_before_and_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rename_calls: list[tuple[str, str]] = []
    stats: list[str] = []
    monkeypatch.setattr(
        "nasmove.smb.smbprotocol_gateway.smbclient.register_session",
        lambda *args, **kwargs: _session(),
    )

    stat_calls = 0

    def stat(path: str, **kwargs: Any) -> SimpleNamespace | None:
        nonlocal stat_calls
        stat_calls += 1
        stats.append(path)
        if stat_calls == 1:
            raise FileNotFoundError(path)
        return SimpleNamespace(st_size=1, st_mode=0o100644, st_mtime_ns=0, st_ino=1)

    def rename(source: str, target: str, **kwargs: Any) -> None:
        rename_calls.append((source, target))
        raise TimeoutError("response lost")

    monkeypatch.setattr("nasmove.smb.smbprotocol_gateway.smbclient.stat", stat)
    monkeypatch.setattr("nasmove.smb.smbprotocol_gateway.smbclient.rename", rename)
    gateway = SmbProtocolGateway()
    gateway.connect(_config(), "secret")

    with pytest.raises(TargetExistsError):
        gateway.rename_exclusive(RemotePath("folder/source"), RemotePath("folder/target"))

    assert rename_calls
    assert stats == [
        r"\\nas.example.test\transfer\folder\target",
        r"\\nas.example.test\transfer\folder\target",
    ]


def test_rename_exclusive_does_not_call_smb_when_target_exists(
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
    gateway.connect(_config(), "secret")
    monkeypatch.setattr(gateway, "stat", lambda path: object())

    with pytest.raises(TargetExistsError):
        gateway.rename_exclusive(RemotePath("folder/source"), RemotePath("folder/target"))

    assert rename_calls == []
