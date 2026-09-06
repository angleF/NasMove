import pytest

pytestmark = pytest.mark.synology


def test_disconnect_harness_is_explicitly_not_run_without_isolated_nas(synology_fixture) -> None:
    result = synology_fixture.transfer_with_disconnects(
        size_gib=50,
        disconnect_percentages=[5, 13, 21, 34, 42, 55, 63, 76, 84, 93],
    )
    assert result.source_sha256 == result.target_sha256
    assert result.source_exists is True
    assert result.max_replayed_bytes <= 68 * 1024 * 1024
