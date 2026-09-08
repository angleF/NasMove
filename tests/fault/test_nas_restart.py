import pytest

pytestmark = pytest.mark.synology


def test_real_nas_restart_during_write_recovers_from_checkpoint(synology_fixture) -> None:
    result = synology_fixture.transfer_across_nas_restart(size_mib=512)

    assert result.source_sha256 == result.target_sha256
    assert result.source_exists is True
    assert result.max_replayed_bytes <= 68 * 1024 * 1024
