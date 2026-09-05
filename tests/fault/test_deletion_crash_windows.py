from __future__ import annotations

import pytest

from nasmove.core.states import ItemState


def test_crash_after_source_delete_before_state_commit_is_idempotently_recovered(
    deletion_fixture,
) -> None:
    deletion_fixture.repository.fail_done_transition_once = True
    with pytest.raises(RuntimeError, match="injected crash"):
        deletion_fixture.service.delete_verified_source(
            deletion_fixture.item.id,
            deletion_fixture.session,
        )

    assert deletion_fixture.local.source_exists is False
    result = deletion_fixture.service.delete_verified_source(
        deletion_fixture.item.id,
        deletion_fixture.session,
    )
    assert result.already_absent is True
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.DONE


def test_crash_before_source_delete_does_not_bypass_authorization(deletion_fixture) -> None:
    deletion_fixture.local.remove_error = RuntimeError("injected crash")
    with pytest.raises(RuntimeError, match="injected crash"):
        deletion_fixture.service.delete_verified_source(
            deletion_fixture.item.id,
            deletion_fixture.session,
        )
    assert deletion_fixture.local.source_exists is True
    assert deletion_fixture.repository.get_item(deletion_fixture.item.id).state is ItemState.SOURCE_RETAINED
