from __future__ import annotations

import os
import plistlib
from pathlib import Path

BUNDLE = Path("dist/NasMove.app")


def test_release_bundle_exists_with_stable_identifier() -> None:
    assert BUNDLE.exists()
    with (BUNDLE / "Contents" / "Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    assert info["CFBundleIdentifier"] == "com.nasmove.app"


def test_release_bundle_contains_no_plaintext_credentials() -> None:
    assert BUNDLE.exists()
    credential_names = (
        "NASMOVE_PASSWORD",
        "NASMOVE_TEST_SMB_PASSWORD",
        "NASMOVE_ADMIN_SECRET",
    )
    forbidden = [name.encode() for name in credential_names]
    forbidden.extend(
        value.encode()
        for name in credential_names
        if len(value := os.environ.get(name, "")) >= 4
    )
    for file_path in BUNDLE.rglob("*"):
        if file_path.is_file() and file_path.stat().st_size <= 20 * 1024 * 1024:
            content = file_path.read_bytes()
            assert all(token not in content for token in forbidden), file_path
