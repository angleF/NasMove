import os

import pytest

pytestmark = pytest.mark.synology


@pytest.mark.skipif(
    os.environ.get("NASMOVE_TEST_SYNOLOGY_SCALE") != "1",
    reason="set NASMOVE_TEST_SYNOLOGY_SCALE=1 for the many-small-files gate",
)
def test_100_000_small_files_complete_with_bounded_memory(synology_fixture) -> None:
    count = int(os.environ.get("NASMOVE_TEST_SMALL_FILE_COUNT", "100000"))
    sample_count = min(1000, count)

    result = synology_fixture.transfer_many_small_files(
        file_count=count,
        sample_count=sample_count,
    )

    assert result.completed_files == count
    assert result.verified_samples == sample_count
    assert result.peak_memory_bytes <= 512 * 1024 * 1024
    assert result.source_files_retained == count
