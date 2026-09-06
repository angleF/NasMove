import pytest

pytestmark = pytest.mark.synology


def test_large_file_transfer_requires_isolated_synology(synology_fixture) -> None:
    pytest.skip("large-file gate requires an approved NAS harness")
