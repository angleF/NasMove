import pytest

pytestmark = pytest.mark.synology


def test_target_race_window_requires_isolated_operator_harness(synology_fixture) -> None:
    pytest.skip("target race injection requires a disposable NAS test share")
