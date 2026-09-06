import pytest

pytestmark = pytest.mark.synology


def test_nas_restart_window_requires_isolated_operator_harness(synology_fixture) -> None:
    pytest.skip("NAS restart requires an operator-approved DSM procedure")
