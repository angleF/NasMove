from dataclasses import replace


def test_full_verification_hashes_source_and_remote_from_zero(verification_fixture) -> None:
    verification_fixture.source_bytes = b"verified payload"
    verification_fixture.remote_bytes = b"verified payload"
    result = verification_fixture.verifier.verify_full(verification_fixture.item)
    assert result.matches is True
    assert result.source_bytes == len(b"verified payload")
    assert result.remote_bytes == len(b"verified payload")
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
