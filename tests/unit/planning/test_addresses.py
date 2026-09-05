import pytest

from nasmove.core.errors import InvalidRemotePath
from nasmove.planning.addresses import parse_smb_address
from nasmove.planning.paths import normalize_remote_path


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("192.168.1.20/archive/photos", "192.168.1.20"),
        ("[2001:db8::20]/archive/photos", "2001:db8::20"),
        ("nas.example.test/archive/photos", "nas.example.test"),
        ("smb://nas.local/archive/photos/2026", "nas.local"),
    ],
)
def test_smb_address_supports_common_hosts(value: str, host: str) -> None:
    parsed = parse_smb_address(value)

    assert parsed.host == host
    assert parsed.share == "archive"
    assert parsed.initial_path == "photos" if value.endswith("photos") else "photos/2026"


def test_smb_url_is_split_into_host_share_and_path() -> None:
    parsed = parse_smb_address("smb://nas.local/archive/photos/2026")

    assert parsed.host == "nas.local"
    assert parsed.share == "archive"
    assert parsed.initial_path == "photos/2026"


def test_parent_escape_is_rejected() -> None:
    with pytest.raises(InvalidRemotePath):
        normalize_remote_path("archive/../../private")
