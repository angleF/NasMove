import pytest

pytestmark = pytest.mark.synology


def test_128_mib_file_recovers_after_repeated_tcp_disconnects(synology_fixture) -> None:
    result = synology_fixture.transfer_with_disconnects(
        size_mib=128,
        disconnect_percentages=[20, 50, 80],
    )
    assert result.source_sha256 == result.target_sha256
    assert result.source_exists is True
    assert result.disconnects == 3
    assert result.max_replayed_bytes <= 68 * 1024 * 1024
