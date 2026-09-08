from __future__ import annotations

from dataclasses import replace

import pytest

from nasmove.core.model import Checkpoint
from nasmove.core.states import SourceKind
from nasmove.localio.hashing import sha256_range
from nasmove.transfer.recovery import RecoveryDisposition


def test_recovery_falls_back_until_window_hash_matches(recovery_fixture) -> None:
    recovery_fixture.remote_size = 128 * 1024 * 1024
    recovery_fixture.set_window_match(offset=128 * 1024 * 1024, matches=False)
    recovery_fixture.set_window_match(offset=64 * 1024 * 1024, matches=True)
    decision = recovery_fixture.coordinator.find_safe_offset(recovery_fixture.item_id)
    assert decision.safe_offset == 64 * 1024 * 1024
    assert decision.truncate_remote is True
    assert decision.disposition is RecoveryDisposition.RESUME


def test_recovery_reports_source_change_without_touching_remote(recovery_fixture) -> None:
    recovery_fixture.local_changed = True
    decision = recovery_fixture.coordinator.find_safe_offset(recovery_fixture.item_id)
    assert decision.disposition is RecoveryDisposition.SOURCE_CHANGED
    assert decision.safe_offset == 0
    assert recovery_fixture.remote_actions == []


def test_recovery_isolates_untrusted_temporary_file(recovery_fixture) -> None:
    recovery_fixture.remote_size = 64 * 1024 * 1024
    recovery_fixture.set_window_match(offset=64 * 1024 * 1024, matches=False)
    decision = recovery_fixture.coordinator.find_safe_offset(recovery_fixture.item_id)
    assert decision.disposition is RecoveryDisposition.START_OVER
    assert decision.safe_offset == 0
    assert len(recovery_fixture.isolated_paths) == 1


def test_recovery_accepts_final_file_only_after_full_hash(recovery_fixture) -> None:
    recovery_fixture.install_final_file(correct=True)
    decision = recovery_fixture.coordinator.find_safe_offset(recovery_fixture.item_id)
    assert decision.disposition is RecoveryDisposition.FINAL_CONFIRMED
    assert decision.safe_offset == recovery_fixture.item.source_fingerprint.size


def test_recovery_does_not_accept_checkpoint_beyond_remote_length(recovery_fixture) -> None:
    recovery_fixture.remote_size = 32 * 1024 * 1024
    recovery_fixture.set_window_match(offset=64 * 1024 * 1024, matches=True)
    decision = recovery_fixture.coordinator.find_safe_offset(recovery_fixture.item_id)
    assert decision.disposition is RecoveryDisposition.START_OVER
    assert decision.safe_offset == 0


@pytest.mark.parametrize(
    ("window_start", "window_length"),
    [
        (124 * 1024 * 1024, 1 * 1024 * 1024),  # window does not end at offset
        (120 * 1024 * 1024, 8 * 1024 * 1024),  # window exceeds the 4 MiB maximum
        (128 * 1024 * 1024, 0),  # empty window after a non-zero offset
    ],
)
def test_recovery_skips_non_canonical_checkpoint_windows(
    recovery_fixture, window_start: int, window_length: int
) -> None:
    recovery_fixture.remote_size = 128 * 1024 * 1024
    recovery_fixture.set_window_match(offset=64 * 1024 * 1024, matches=True)
    with recovery_fixture.local.open_read(recovery_fixture.item.source_path) as source:
        digest = sha256_range(source, window_start, window_length)
    recovery_fixture.repository.checkpoints.append(
        Checkpoint(
            item_id=recovery_fixture.item_id,
            confirmed_offset=128 * 1024 * 1024,
            remote_size=128 * 1024 * 1024,
            window_start=window_start,
            window_length=window_length,
            window_sha256=digest,
            session_generation=1,
        )
    )
    decision = recovery_fixture.coordinator.find_safe_offset(recovery_fixture.item_id)
    assert decision.safe_offset == 64 * 1024 * 1024
    assert decision.disposition is RecoveryDisposition.RESUME


def test_recovery_reuses_unique_temporary_empty_directory(recovery_fixture) -> None:
    item = recovery_fixture.repository.get_item(recovery_fixture.item_id)
    item = replace(
        item,
        source_fingerprint=replace(
            item.source_fingerprint, kind=SourceKind.EMPTY_DIRECTORY, size=0
        ),
        confirmed_offset=0,
    )
    recovery_fixture.repository.items[item.id] = item
    recovery_fixture.local.fingerprint_override = item.source_fingerprint
    recovery_fixture.remote.directories.add(item.temp_path.value)

    decision = recovery_fixture.coordinator.find_safe_offset(item.id)

    assert decision.disposition is RecoveryDisposition.RESUME
    assert decision.safe_offset == 0
