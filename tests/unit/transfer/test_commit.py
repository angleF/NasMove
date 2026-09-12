import hashlib
from dataclasses import replace

import pytest

from nasmove.core.errors import ConflictResolutionRequired
from nasmove.core.states import ConflictPolicy, SourceKind
from nasmove.transfer.verification import VerificationResult


def test_commit_allocates_next_name_when_planned_name_was_taken(commit_fixture) -> None:
    commit_fixture.occupy("movie.mov")
    result = commit_fixture.committer.commit(
        commit_fixture.item, commit_fixture.valid_verification()
    )
    assert str(result.final_path).endswith("movie (1).mov")
    assert commit_fixture.repository.get_item(commit_fixture.item.id).final_path == result.final_path


def test_commit_atomically_replaces_existing_target_for_overwrite_policy(
    commit_fixture,
) -> None:
    commit_fixture.occupy("movie.mov")

    result = commit_fixture.committer.commit(
        commit_fixture.item,
        commit_fixture.valid_verification(),
        conflict_policy=ConflictPolicy.OVERWRITE,
    )

    assert result.final_path == commit_fixture.item.final_path
    assert bytes(commit_fixture.remote.files["target/movie.mov"]) == b"movie payload"
    assert "remote.replace@target/movie.mov" in commit_fixture.remote.trace


def test_commit_skip_policy_preserves_racing_target(commit_fixture) -> None:
    commit_fixture.occupy("movie.mov")
    commit_fixture.remote.files["target/movie.mov"] = bytearray(b"occupied")

    result = commit_fixture.committer.commit(
        commit_fixture.item,
        commit_fixture.valid_verification(),
        conflict_policy=ConflictPolicy.SKIP,
    )

    assert result.committed is False
    assert commit_fixture.repository.get_item(commit_fixture.item.id).state.value == "skipped"
    assert bytes(commit_fixture.remote.files["target/movie.mov"]) == b"occupied"


@pytest.mark.parametrize(("delta", "committed"), ((-1, True), (1, False)))
def test_commit_overwrite_if_newer_rechecks_racing_target_mtime(
    commit_fixture, delta: int, committed: bool
) -> None:
    commit_fixture.occupy("movie.mov")
    commit_fixture.remote.modified_ns = (
        commit_fixture.item.source_fingerprint.mtime_ns + delta
    )

    result = commit_fixture.committer.commit(
        commit_fixture.item,
        commit_fixture.valid_verification(),
        conflict_policy=ConflictPolicy.OVERWRITE_IF_NEWER,
    )

    assert result.committed is committed


def test_commit_ask_policy_never_guesses_when_target_races(commit_fixture) -> None:
    commit_fixture.occupy("movie.mov")

    with pytest.raises(ConflictResolutionRequired):
        commit_fixture.committer.commit(
            commit_fixture.item,
            commit_fixture.valid_verification(),
            conflict_policy=ConflictPolicy.ASK,
        )

    assert commit_fixture.repository.get_item(commit_fixture.item.id).state.value == "verified"
    assert commit_fixture.item.temp_path.value in commit_fixture.remote.files


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


def test_empty_directory_commit_renames_verified_temporary_directory(commit_fixture) -> None:
    item = replace(
        commit_fixture.item,
        source_fingerprint=replace(
            commit_fixture.item.source_fingerprint,
            kind=SourceKind.EMPTY_DIRECTORY,
            size=0,
        ),
    )
    commit_fixture.remote.files.pop(item.temp_path.value)
    commit_fixture.remote.directories.add(item.temp_path.value)
    commit_fixture.repository.items[item.id] = item
    digest = hashlib.sha256().hexdigest()
    verification = VerificationResult(
        matches=True,
        source_unchanged=True,
        source_hash=digest,
        remote_hash=digest,
        source_bytes=0,
        remote_bytes=0,
        session_generation=1,
        remote_file_id=commit_fixture.remote.stat(item.temp_path).file_id,
    )

    result = commit_fixture.committer.commit(item, verification)

    assert result.final_path.value in commit_fixture.remote.directories
    assert item.temp_path.value not in commit_fixture.remote.directories
