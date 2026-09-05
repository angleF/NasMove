from __future__ import annotations

from nasmove.transfer.checkpoint_writer import CHECKPOINT_BYTES, IO_BLOCK_BYTES, CheckpointWriter


def test_flush_precedes_checkpoint_commit(fake_dependencies) -> None:
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())
    result = writer.copy(
        item=fake_dependencies.item(size=CHECKPOINT_BYTES + IO_BLOCK_BYTES),
        start_offset=0,
        session=fake_dependencies.session(generation=3),
    )
    assert result.bytes_copied == CHECKPOINT_BYTES + IO_BLOCK_BYTES
    assert fake_dependencies.trace.index("remote.flush@67108864") < fake_dependencies.trace.index(
        "repository.save_checkpoint@67108864"
    )


def test_copy_uses_four_mebibyte_blocks_and_final_checkpoint(fake_dependencies) -> None:
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())
    result = writer.copy(
        item=fake_dependencies.item(size=IO_BLOCK_BYTES + 7),
        start_offset=0,
        session=fake_dependencies.session(generation=1),
    )

    assert result.bytes_copied == IO_BLOCK_BYTES + 7
    assert fake_dependencies.repository.checkpoints[-1].confirmed_offset == IO_BLOCK_BYTES + 7


def test_short_remote_writes_are_completed_before_checkpoint(fake_dependencies) -> None:
    fake_dependencies.remote.short_write = True
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())

    result = writer.copy(
        item=fake_dependencies.item(size=IO_BLOCK_BYTES + 7),
        start_offset=0,
        session=fake_dependencies.session(generation=1),
    )

    assert result.outcome.value == "completed"
    assert bytes(fake_dependencies.remote.files["target/.file.bin.part"]) == fake_dependencies.local.content


def test_resume_starts_at_the_last_durable_offset(fake_dependencies) -> None:
    fake_dependencies.local.content = fake_dependencies.local.content[: IO_BLOCK_BYTES * 2]
    item = fake_dependencies.item(size=IO_BLOCK_BYTES * 2)
    fake_dependencies.remote.files[item.temp_path.value] = bytearray(
        fake_dependencies.local.content[:IO_BLOCK_BYTES]
    )
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())

    result = writer.copy(
        item=item,
        start_offset=IO_BLOCK_BYTES,
        session=fake_dependencies.session(generation=1),
    )

    assert result.outcome.value == "completed"
    assert result.confirmed_offset == IO_BLOCK_BYTES * 2
    assert bytes(fake_dependencies.remote.files[item.temp_path.value]) == fake_dependencies.local.content


def test_pause_finishes_current_block_and_persists_checkpoint(fake_dependencies) -> None:
    fake_dependencies.cancellation_token.pause_requested = True
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())

    result = writer.copy(
        item=fake_dependencies.item(size=IO_BLOCK_BYTES * 2),
        start_offset=0,
        session=fake_dependencies.session(generation=1),
    )

    assert result.outcome.value == "paused"
    assert result.bytes_copied == IO_BLOCK_BYTES
    assert fake_dependencies.repository.checkpoints[-1].confirmed_offset == IO_BLOCK_BYTES
