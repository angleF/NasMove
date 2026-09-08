import hashlib
from dataclasses import replace

import pytest

from nasmove.core.states import SourceKind
from nasmove.transfer.verification import VerificationResult


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
