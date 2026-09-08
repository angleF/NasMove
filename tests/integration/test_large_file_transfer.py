import os

import pytest

pytestmark = pytest.mark.synology


@pytest.mark.skipif(
    os.environ.get("NASMOVE_TEST_SYNOLOGY_LARGE") != "1",
    reason="set NASMOVE_TEST_SYNOLOGY_LARGE=1 for the 50 GiB gate",
)
def test_50_gib_file_survives_ten_disconnects(synology_fixture) -> None:
    result = synology_fixture.transfer_with_disconnects(
        size_gib=50,
        disconnect_percentages=[5, 13, 21, 34, 42, 55, 63, 76, 84, 93],
    )

    assert result.source_sha256 == result.target_sha256
    assert result.source_exists is True
    assert result.max_replayed_bytes <= 68 * 1024 * 1024
