from __future__ import annotations

import unicodedata

from nasmove.core.errors import InvalidRemotePath
from nasmove.core.model import RemotePath


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
    try:
        return RemotePath(normalized)
    except ValueError as exc:
        raise InvalidRemotePath(str(exc)) from exc
