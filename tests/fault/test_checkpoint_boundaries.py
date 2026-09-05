from __future__ import annotations

import pytest

from nasmove.transfer.checkpoint_writer import CHECKPOINT_BYTES, CheckpointWriter


@pytest.mark.parametrize(
    "failure_point", ["write", "flush_before", "flush_after", "stat", "save"]
)
def test_failure_never_reports_uncommitted_checkpoint(fake_dependencies, failure_point: str) -> None:
    remote = fake_dependencies.remote
    if failure_point == "write":
        remote.fail_write = True
    elif failure_point == "flush_before":
        remote.fail_flush = True
    elif failure_point == "flush_after":
        remote.fail_flush_after_write = True
    elif failure_point == "stat":
        remote.fail_stat = True
    else:
        def fail_save(*_args, **_kwargs):
            raise OSError("save failure")

        fake_dependencies.repository.save_checkpoint = fail_save  # type: ignore[method-assign]

    writer = CheckpointWriter(**fake_dependencies.as_kwargs())
    result = writer.copy(
        item=fake_dependencies.item(size=CHECKPOINT_BYTES),
        start_offset=0,
        session=fake_dependencies.session(generation=1),
    )

    assert result.outcome.value == "interrupted"
    assert result.confirmed_offset == 0
    assert all(checkpoint.confirmed_offset == 0 for checkpoint in fake_dependencies.repository.checkpoints)


def test_database_failure_after_a_committed_checkpoint_keeps_previous_offset(fake_dependencies) -> None:
    original_save = fake_dependencies.repository.save_checkpoint

    def fail_after_first(checkpoint) -> None:
        if checkpoint.confirmed_offset > CHECKPOINT_BYTES:
            raise OSError("commit failure")
        original_save(checkpoint)

    fake_dependencies.repository.save_checkpoint = fail_after_first  # type: ignore[method-assign]
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())

    result = writer.copy(
        item=fake_dependencies.item(size=CHECKPOINT_BYTES + 1),
        start_offset=0,
        session=fake_dependencies.session(generation=1),
    )

    assert result.outcome.value == "interrupted"
    assert result.confirmed_offset == CHECKPOINT_BYTES
    assert [cp.confirmed_offset for cp in fake_dependencies.repository.checkpoints] == [
        CHECKPOINT_BYTES
    ]
