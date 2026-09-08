from __future__ import annotations

import subprocess

import pytest

from tests.fixtures.synology import _SynologyAdmin


def test_temporary_user_secret_is_sent_via_environment_not_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    admin = _SynologyAdmin("TestShare", "NasMoveTest/probe")

    admin.create_test_user("nasmove_test_1234", "private-temp-password")

    command = str(captured["env"]["NASMOVE_ADMIN_COMMAND"])  # type: ignore[index]
    assert "private-temp-password" not in command
    assert "NASMOVE_TEMP_SECRET" not in command
    assert captured["env"]["NASMOVE_ADMIN_SECRET"] == "private-temp-password"  # type: ignore[index]


@pytest.mark.parametrize("username", ["", "../admin", "name with space", "a/b"])
def test_temporary_user_rejects_unsafe_username(username: str) -> None:
    admin = _SynologyAdmin("TestShare", "NasMoveTest/probe")

    with pytest.raises(ValueError, match="temporary username"):
        admin.create_test_user(username, "secret")


def test_reboot_accepts_ssh_disconnect_after_command_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=85),
    )
    admin = _SynologyAdmin("TestShare", "NasMoveTest/probe")

    admin.reboot()
