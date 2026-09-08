import pytest

pytestmark = pytest.mark.synology


def test_real_free_space_preflight_rejects_oversized_sparse_source(synology_fixture) -> None:
    result = synology_fixture.reject_plan_beyond_free_space()

    assert result.source_exists is True
    assert result.remote_root_unchanged is True
    assert result.error_code == "insufficient_space"


def test_real_permission_revocation_fails_without_deleting_source(synology_fixture) -> None:
    result = synology_fixture.transfer_into_revoked_directory()

    assert result.error_category == "permission", (result.error_type, result.error_code)
    assert result.source_exists is True
    assert result.final_target_exists is False


def test_real_mid_transfer_disk_full_retains_source(synology_fixture) -> None:
    result = synology_fixture.transfer_into_small_tmpfs()

    assert result.error_category in {"disk_full", "quota"}, (
        result.error_type,
        result.error_code,
    )
    assert result.source_exists is True
    assert result.final_target_exists is False
