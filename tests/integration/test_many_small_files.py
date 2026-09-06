import pytest

pytestmark = pytest.mark.synology


def test_many_small_files_requires_isolated_synology(synology_fixture) -> None:
    pytest.skip("many-small-files gate requires an approved NAS harness")
