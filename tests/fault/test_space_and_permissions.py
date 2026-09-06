import pytest

pytestmark = pytest.mark.synology


def test_space_and_permission_windows_require_isolated_operator_harness(synology_fixture) -> None:
    pytest.skip("quota and permission changes require an operator-approved DSM procedure")
