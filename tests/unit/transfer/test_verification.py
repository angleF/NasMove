import hashlib
from dataclasses import replace

import pytest

from nasmove.core.states import SourceKind
from tests.fixtures.transfer import TransferToken


def test_full_verification_hashes_source_and_remote_from_zero(verification_fixture) -> None:
    verification_fixture.source_bytes = b"verified payload"
    verification_fixture.remote_bytes = b"verified payload"
    result = verification_fixture.verifier.verify_full(verification_fixture.item)
    assert result.matches is True
    assert result.source_bytes == len(b"verified payload")
    assert result.remote_bytes == len(b"verified payload")
    assert verification_fixture.remote.remote_read_started_at == 0


def test_full_verification_stops_at_source_hash_block_boundary(verification_fixture) -> None:
    token = TransferToken()
    verification_fixture.local.cancel_token = token

    with pytest.raises(InterruptedError, match="verification stopped"):
        verification_fixture.verifier.verify_full(verification_fixture.item, token=token)

    assert token.cancel_requested is True
    assert verification_fixture.remote.remote_read_started_at is None


def test_full_verification_stops_at_remote_hash_block_boundary(verification_fixture) -> None:
    token = TransferToken()

    def request_cancel(_item, _offset) -> None:
        token.request_cancel()

    verification_fixture.verifier._progress = request_cancel

    with pytest.raises(InterruptedError, match="verification stopped"):
        verification_fixture.verifier.verify_full(verification_fixture.item, token=token)

    assert token.cancel_requested is True
    assert verification_fixture.remote.remote_read_started_at == 0


def test_full_verification_requires_exact_length_and_hash(verification_fixture) -> None:
    verification_fixture.remote_bytes = b"verified payload with trailing bytes"
    result = verification_fixture.verifier.verify_full(verification_fixture.item)
    assert result.matches is False
    assert result.source_bytes != result.remote_bytes


def test_full_verification_reports_source_change(verification_fixture) -> None:
    verification_fixture.local.fingerprint_override = replace(
        verification_fixture.item.source_fingerprint, mtime_ns=2
    )
    result = verification_fixture.verifier.verify_full(verification_fixture.item)
    assert result.matches is False
    assert result.source_unchanged is False


def test_full_verification_reports_missing_remote_as_mismatch(verification_fixture) -> None:
    verification_fixture.remote.files.clear()
    result = verification_fixture.verifier.verify_full(verification_fixture.item)
    assert result.matches is False
    assert result.remote_bytes == 0


def test_full_verification_rejects_same_length_remote_replacement(verification_fixture) -> None:
    verification_fixture.remote.replace_after_read = True
    verification_fixture.remote.replacement_content = b"changed payload!"
    result = verification_fixture.verifier.verify_full(verification_fixture.item)
    assert result.matches is False


def test_full_verification_rejects_different_length_replacement_without_file_id(
    verification_fixture,
) -> None:
    verification_fixture.remote.replace_after_read = True
    verification_fixture.remote.replacement_without_file_id = True
    verification_fixture.remote.replacement_content = b"different length content"
    verification_fixture.remote.file_ids[verification_fixture.item.temp_path.value] = None
    result = verification_fixture.verifier.verify_full(verification_fixture.item)
    assert result.matches is False


def test_empty_directory_verification_checks_local_fingerprint_and_remote_temp_directory(
    verification_fixture,
) -> None:
    item = replace(
        verification_fixture.item,
        source_fingerprint=replace(
            verification_fixture.item.source_fingerprint,
            kind=SourceKind.EMPTY_DIRECTORY,
            size=0,
        ),
    )
    verification_fixture.local.fingerprint_override = item.source_fingerprint
    verification_fixture.remote.files.pop(item.temp_path.value)
    verification_fixture.remote.directories.add(item.temp_path.value)

    result = verification_fixture.verifier.verify_full(item)

    assert result.matches is True
    assert result.source_hash == hashlib.sha256().hexdigest()
    assert result.source_bytes == result.remote_bytes == 0
