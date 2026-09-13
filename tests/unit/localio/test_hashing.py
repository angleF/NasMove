from __future__ import annotations

import hashlib
import io

import pytest

from nasmove.localio.hashing import HashResult, sha256_range, sha256_stream


def test_stream_hash_reads_until_eof() -> None:
    payload = b"abc" * 1_000_000

    result = sha256_stream(io.BytesIO(payload), block_size=4096)

    assert result == HashResult(hashlib.sha256(payload).hexdigest(), len(payload))


def test_stream_hash_handles_empty_stream() -> None:
    result = sha256_stream(io.BytesIO())

    assert result.byte_count == 0
    assert result.hexdigest == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("block_size", [0, -1, True, False])
def test_stream_hash_rejects_invalid_block_size(block_size: object) -> None:
    with pytest.raises(ValueError):
        sha256_stream(io.BytesIO(b"payload"), block_size=block_size)  # type: ignore[arg-type]


def test_range_hashes_exact_requested_bytes() -> None:
    payload = b"0123456789"
    stream = io.BytesIO(payload)

    digest = sha256_range(stream, start=2, length=5)

    assert digest == hashlib.sha256(payload[2:7]).hexdigest()
    assert stream.tell() == 7


def test_range_hash_supports_empty_range_at_eof() -> None:
    stream = io.BytesIO(b"payload")

    digest = sha256_range(stream, start=7, length=0)

    assert digest == hashlib.sha256(b"").hexdigest()
    assert stream.tell() == 7


@pytest.mark.parametrize(
    ("start", "length"),
    [(-1, 1), (0, -1), (True, 1), (0, False), (8, 0), (7, 1)],
)
def test_range_hash_rejects_invalid_or_out_of_bounds_ranges(start: object, length: object) -> None:
    with pytest.raises(ValueError):
        sha256_range(io.BytesIO(b"payload"), start=start, length=length)  # type: ignore[arg-type]


def test_range_hash_rejects_short_stream() -> None:
    with pytest.raises(ValueError):
        sha256_range(io.BytesIO(b"payload"), start=4, length=10)


def test_range_hash_with_explicit_stream_size() -> None:
    payload = b"0123456789"
    stream = io.BytesIO(payload)
    digest = sha256_range(stream, start=2, length=5, stream_size=len(payload))
    assert digest == hashlib.sha256(payload[2:7]).hexdigest()
    assert stream.tell() == 7


def test_range_hash_rejects_invalid_stream_size() -> None:
    with pytest.raises(ValueError):
        sha256_range(io.BytesIO(b"payload"), start=0, length=4, stream_size=-1)
    with pytest.raises(ValueError):
        sha256_range(io.BytesIO(b"payload"), start=0, length=4, stream_size=2)
