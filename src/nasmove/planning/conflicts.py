from __future__ import annotations

import unicodedata
from collections.abc import Callable


def conflict_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def allocate_name(name: str, occupied: set[str]) -> str:
    """Return ``name`` or the first extension-preserving conflict-free suffix."""
    if "/" in name or "\\" in name or not name or name in {".", ".."}:
        raise ValueError("name must be one remote path component")
    occupied_keys = {conflict_key(candidate) for candidate in occupied}
    if conflict_key(name) not in occupied_keys:
        return name

    stem, extension = _split_extension(name)
    index = 1
    while True:
        candidate = f"{stem} ({index}){extension}"
        if conflict_key(candidate) not in occupied_keys:
            return candidate
        index += 1


def allocate_name_with_index(name: str, is_occupied: Callable[[str], bool]) -> str:
    """Allocate a name using an external occupancy index without materializing it."""
    if "/" in name or "\\" in name or not name or name in {".", ".."}:
        raise ValueError("name must be one remote path component")
    if not is_occupied(conflict_key(name)):
        return name

    stem, extension = _split_extension(name)
    index = 1
    while True:
        candidate = f"{stem} ({index}){extension}"
        if not is_occupied(conflict_key(candidate)):
            return candidate
        index += 1

def _split_extension(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]
