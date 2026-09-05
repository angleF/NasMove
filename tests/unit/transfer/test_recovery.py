from __future__ import annotations

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
