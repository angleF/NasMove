from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO

_DEFAULT_BLOCK_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HashResult:
    hexdigest: str
    byte_count: int


def _validate_non_negative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _read_bytes(stream: BinaryIO, size: int) -> bytes:
    chunk = stream.read(size)
    if not isinstance(chunk, bytes):
        raise TypeError("stream.read() must return bytes")
    return chunk


def sha256_stream(stream: BinaryIO, block_size: int = _DEFAULT_BLOCK_SIZE, *, progress: Callable[[int], None] | None = None) -> HashResult:
    if type(block_size) is not int or block_size <= 0:
        raise ValueError("block_size must be a positive integer")
    digest = hashlib.sha256()
    byte_count = 0
    while True:
        chunk = _read_bytes(stream, block_size)
        if not chunk:
            break
        digest.update(chunk)
        byte_count += len(chunk)
        if progress is not None:
            progress(byte_count)
    return HashResult(hexdigest=digest.hexdigest(), byte_count=byte_count)


def sha256_range(stream: BinaryIO, start: int, length: int) -> str:
    start = _validate_non_negative_int(start, "start")
    length = _validate_non_negative_int(length, "length")

    stream.seek(0, os.SEEK_END)
    end = stream.tell()
    if start > end or length > end - start:
        raise ValueError("requested range is outside the stream")
    stream.seek(start, os.SEEK_SET)

    digest = hashlib.sha256()
    remaining = length
    while remaining:
        chunk = _read_bytes(stream, min(_DEFAULT_BLOCK_SIZE, remaining))
        if not chunk:
            raise ValueError("stream ended before requested range")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()
