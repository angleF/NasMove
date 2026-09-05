import pytest


def test_commit_allocates_next_name_when_planned_name_was_taken(commit_fixture) -> None:
    commit_fixture.occupy("movie.mov")
    result = commit_fixture.committer.commit(
        commit_fixture.item, commit_fixture.valid_verification()
    )
    assert str(result.final_path).endswith("movie (1).mov")
    assert commit_fixture.repository.get_item(commit_fixture.item.id).final_path == result.final_path


def test_commit_rejects_unverified_result(commit_fixture) -> None:
    verification = commit_fixture.valid_verification()
    verification = type(verification)(
        matches=False,
        source_unchanged=True,
        source_hash=verification.source_hash,
        remote_hash=verification.remote_hash,
        source_bytes=verification.source_bytes,
        remote_bytes=verification.remote_bytes,
        session_generation=1,
    )
    with pytest.raises(ValueError):
        commit_fixture.committer.commit(commit_fixture.item, verification)


def test_commit_rejects_same_length_replacement_after_rename(commit_fixture) -> None:
    commit_fixture.remote.replace_after_rename = True
    commit_fixture.remote.replacement_content = b"wrong content"
    with pytest.raises(OSError):
        commit_fixture.committer.commit(
            commit_fixture.item, commit_fixture.valid_verification()
        )
    assert commit_fixture.repository.get_item(commit_fixture.item.id).state.value == "verified"
