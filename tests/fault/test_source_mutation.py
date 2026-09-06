import pytest

pytestmark = pytest.mark.synology


def test_source_mutation_window_requires_isolated_operator_harness(synology_fixture) -> None:
    pytest.skip("source mutation requires a dedicated disposable source tree")
