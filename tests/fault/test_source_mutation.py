import pytest

pytestmark = pytest.mark.synology


def test_real_source_mutation_prevents_commit_and_retains_source(synology_fixture) -> None:
    result = synology_fixture.transfer_while_source_changes()

    assert result.error_code == "full_verification_failed"
    assert result.source_exists is True
    assert result.final_target_exists is False
