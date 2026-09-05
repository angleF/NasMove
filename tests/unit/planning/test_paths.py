import pytest

from nasmove.core.errors import InvalidRemotePath
from nasmove.planning.paths import normalize_remote_path


def test_path_is_normalized_to_posix_remote_path() -> None:
    path = normalize_remote_path("photos/2026/旅行")

    assert path.value == "photos/2026/旅行"


@pytest.mark.parametrize("value", ["", "/archive", "archive/", "archive//photos", "archive/./photos"])
def test_empty_dot_and_absolute_components_are_rejected(value: str) -> None:
    with pytest.raises(InvalidRemotePath):
        normalize_remote_path(value)


def test_backslash_and_control_characters_are_rejected() -> None:
    for value in ("archive\\photos", "archive/line\nfeed"):
        with pytest.raises(InvalidRemotePath):
            normalize_remote_path(value)


@pytest.mark.parametrize("value", ["a<b", "a>b", 'a:b', 'a"b', "a\\b", "a|b", "a?b", "a*b", "a.", "a "])
def test_smb_name_rules_are_rejected(value: str) -> None:
    with pytest.raises(InvalidRemotePath):
        normalize_remote_path(value)


def test_component_and_full_path_lengths_use_utf16_units() -> None:
    normalize_remote_path("😀" * 127)
    with pytest.raises(InvalidRemotePath):
        normalize_remote_path("😀" * 128)
    with pytest.raises(InvalidRemotePath):
        normalize_remote_path("a" * 32767 + "/b")
