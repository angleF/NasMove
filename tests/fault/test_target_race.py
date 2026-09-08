import pytest

pytestmark = pytest.mark.synology


def test_real_target_race_preserves_existing_file_and_auto_renames(synology_fixture) -> None:
    result = synology_fixture.transfer_with_target_race()

    assert result.success is True
    assert result.existing_target_preserved is True
    assert result.final_path_was_renamed is True
    assert result.source_exists is True
