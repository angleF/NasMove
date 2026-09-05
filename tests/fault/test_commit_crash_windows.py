import pytest

from nasmove.transfer.recovery import RecoveryDisposition


def test_crash_before_rename_leaves_only_temporary_file(commit_fixture) -> None:
    commit_fixture.remote.crash_before_rename = True
    with pytest.raises(RuntimeError):
        commit_fixture.committer.commit(
            commit_fixture.item, commit_fixture.valid_verification()
        )
    assert "target/.file.bin.part" in commit_fixture.remote.files
    assert "target/movie.mov" not in commit_fixture.remote.files


def test_crash_after_rename_requires_recovery_reverification(commit_fixture) -> None:
    commit_fixture.remote.crash_after_rename = True
    with pytest.raises(RuntimeError):
        commit_fixture.committer.commit(
            commit_fixture.item, commit_fixture.valid_verification()
        )
    from nasmove.transfer.recovery import RecoveryCoordinator

    decision = RecoveryCoordinator(
        commit_fixture.repository,
        commit_fixture.local,
        commit_fixture.remote,
        session_generation=1,
    ).find_safe_offset(commit_fixture.item.id)
    assert decision.disposition is RecoveryDisposition.FINAL_CONFIRMED
