from __future__ import annotations

import os

import pytest

from nasmove.core.model import ConnectionConfig, ConnectionProfileId, RemotePath
from nasmove.smb.capability_probe import SmbCapabilityProbe
from nasmove.smb.smbprotocol_gateway import SmbProtocolGateway

_REQUIRED_ENVIRONMENT = (
    "NASMOVE_TEST_SMB_HOST",
    "NASMOVE_TEST_SMB_SHARE",
    "NASMOVE_TEST_SMB_USERNAME",
    "NASMOVE_TEST_SMB_PASSWORD",
    "NASMOVE_TEST_SMB_TARGET",
)


def _integration_settings() -> dict[str, str]:
    if os.environ.get("NASMOVE_TEST_SMB") != "1":
        pytest.skip("set NASMOVE_TEST_SMB=1 to enable the dedicated SMB integration test")
    missing = [name for name in _REQUIRED_ENVIRONMENT if not os.environ.get(name)]
    if missing:
        pytest.skip(f"dedicated SMB test environment is incomplete: {', '.join(missing)}")
    return {name: os.environ[name] for name in _REQUIRED_ENVIRONMENT}


def test_dedicated_smb_share_supports_required_durability_operations() -> None:
    settings = _integration_settings()
    gateway = SmbProtocolGateway()
    config = ConnectionConfig(
        profile_id=ConnectionProfileId("integration-probe"),
        display_name="Dedicated SMB integration probe",
        host=settings["NASMOVE_TEST_SMB_HOST"],
        port=int(os.environ.get("NASMOVE_TEST_SMB_PORT", "445")),
        share=settings["NASMOVE_TEST_SMB_SHARE"],
        username=settings["NASMOVE_TEST_SMB_USERNAME"],
        domain=os.environ.get("NASMOVE_TEST_SMB_DOMAIN") or None,
        require_encryption=os.environ.get("NASMOVE_TEST_SMB_ALLOW_UNENCRYPTED") != "1",
    )
    target = RemotePath(settings["NASMOVE_TEST_SMB_TARGET"])

    try:
        gateway.connect(config, settings["NASMOVE_TEST_SMB_PASSWORD"])
        report = SmbCapabilityProbe(gateway).run(target)
        entries = gateway.list_dir(target)
    finally:
        gateway.disconnect()

    assert report.all_supported is True
    assert not any(entry.name.startswith(".nasmove-probe-") for entry in entries)
