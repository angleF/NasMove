from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from tests.fixtures.fault_proxy import FaultProxy


@dataclass(frozen=True, slots=True)
class SynologyResult:
    source_sha256: str
    target_sha256: str
    source_exists: bool
    max_replayed_bytes: int


class SynologyFixture:
    """Validated environment boundary for an isolated test share."""

    def __init__(self) -> None:
        self.host = os.environ["NASMOVE_SYNOLOGY_HOST"]
        self.share = os.environ["NASMOVE_SYNOLOGY_SHARE"]
        self.test_root = os.environ["NASMOVE_SYNOLOGY_TEST_ROOT"]
        if not self.test_root.startswith("NasMoveTest/"):
            raise ValueError("NASMOVE_SYNOLOGY_TEST_ROOT must be under NasMoveTest/")
        self.fault_proxy = FaultProxy()

    def transfer_with_disconnects(self, **_: object) -> SynologyResult:
        raise NotImplementedError("real Synology transfer harness is not configured")


@pytest.fixture
def synology_fixture() -> SynologyFixture:
    if os.environ.get("NASMOVE_TEST_SYNOLOGY") != "1":
        pytest.skip("set NASMOVE_TEST_SYNOLOGY=1 for the isolated Synology gate")
    if os.environ.get("NASMOVE_TEST_SYNOLOGY_READY") != "1":
        pytest.skip("set NASMOVE_TEST_SYNOLOGY_READY=1 after the test-share checklist")
    return SynologyFixture()
