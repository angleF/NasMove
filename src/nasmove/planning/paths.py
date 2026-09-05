from __future__ import annotations

import unicodedata

from nasmove.core.errors import InvalidRemotePath
from nasmove.core.model import RemotePath

MAX_COMPONENT_UTF16_UNITS = 255
MAX_PATH_UTF16_UNITS = 32767
_FORBIDDEN_NAME_CHARACTERS = frozenset('<>:"/\\|?*')


def normalize_remote_path(value: str) -> RemotePath:
    """Validate and normalize a path relative to the configured SMB share."""
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise InvalidRemotePath("remote path must be a relative slash-separated path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InvalidRemotePath("remote path must not contain control characters")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise InvalidRemotePath("remote path must not contain empty, dot, or dot-dot segments")
    normalized = "/".join(unicodedata.normalize("NFC", component) for component in components)
    for component in normalized.split("/"):
        validate_remote_component(component)
    if _utf16_units(normalized) > MAX_PATH_UTF16_UNITS:
        raise InvalidRemotePath("remote path exceeds the conservative UTF-16 path limit")
    try:
        return RemotePath(normalized)
    except ValueError as exc:
        raise InvalidRemotePath(str(exc)) from exc


def validate_remote_component(value: str) -> str:
    if not value or value in {".", ".."}:
        raise InvalidRemotePath("remote path component must not be empty or dot-like")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InvalidRemotePath("remote path component must not contain control characters")
    if any(char in _FORBIDDEN_NAME_CHARACTERS for char in value):
        raise InvalidRemotePath("remote path component contains a forbidden SMB character")
    if value[-1] in {".", " "}:
        raise InvalidRemotePath("remote path component must not end with dot or space")
    if _utf16_units(value) > MAX_COMPONENT_UTF16_UNITS:
        raise InvalidRemotePath("remote path component exceeds 255 UTF-16 units")
    return value


def _utf16_units(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise InvalidRemotePath("remote path contains an invalid Unicode scalar") from exc
