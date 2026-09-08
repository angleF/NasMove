from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from nasmove.core.model import RemotePath
from nasmove.core.states import SourceKind
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


def test_local_window_hash_precedes_flush_stat_and_checkpoint_save(fake_dependencies) -> None:
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())
    writer.copy(
        item=fake_dependencies.item(size=CHECKPOINT_BYTES),
        start_offset=0,
        session=fake_dependencies.session(generation=3),
    )
    hash_index = fake_dependencies.trace.index("local.hash")
    flush_index = fake_dependencies.trace.index("remote.flush@67108864")
    stat_index = fake_dependencies.trace.index("remote.stat@67108864")
    save_index = fake_dependencies.trace.index("repository.save_checkpoint@67108864")
    assert hash_index < flush_index < stat_index < save_index


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
    item = fake_dependencies.item(size=IO_BLOCK_BYTES * 2, confirmed_offset=IO_BLOCK_BYTES)
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


def test_start_offset_above_item_checkpoint_is_rejected(fake_dependencies) -> None:
    item = fake_dependencies.item(size=IO_BLOCK_BYTES * 2, confirmed_offset=IO_BLOCK_BYTES)
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())

    with pytest.raises(ValueError, match="durable item checkpoint"):
        writer.copy(
            item=item,
            start_offset=IO_BLOCK_BYTES * 2,
            session=fake_dependencies.session(generation=1),
        )


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


def test_cancel_stops_at_block_boundary_without_checkpointing_pending_block(fake_dependencies) -> None:
    fake_dependencies.local.cancel_token = fake_dependencies.cancellation_token
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())

    result = writer.copy(
        item=fake_dependencies.item(size=IO_BLOCK_BYTES),
        start_offset=0,
        session=fake_dependencies.session(generation=1),
    )

    assert result.outcome.value == "cancelled"
    assert result.confirmed_offset == 0
    assert fake_dependencies.repository.checkpoints == []
    assert not any(entry.startswith("remote.flush") for entry in fake_dependencies.trace)
    assert not any(entry.startswith("repository.save_checkpoint") for entry in fake_dependencies.trace)


def test_cancel_on_eof_skips_final_checkpoint(fake_dependencies) -> None:
    fake_dependencies.local.cancel_token = fake_dependencies.cancellation_token
    fake_dependencies.local.cancel_on_eof = True
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())

    result = writer.copy(
        item=fake_dependencies.item(size=IO_BLOCK_BYTES),
        start_offset=0,
        session=fake_dependencies.session(generation=1),
    )

    assert result.outcome.value == "cancelled"
    assert result.confirmed_offset == 0
    assert fake_dependencies.repository.checkpoints == []


def test_repository_save_runs_inside_session_lease(fake_dependencies) -> None:
    original_save = fake_dependencies.repository.save_checkpoint

    def save_with_switch_request(checkpoint) -> None:
        assert fake_dependencies.remote.request_generation_switch() is False
        original_save(checkpoint)

    fake_dependencies.repository.save_checkpoint = save_with_switch_request  # type: ignore[method-assign]
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())

    result = writer.copy(
        item=fake_dependencies.item(size=IO_BLOCK_BYTES),
        start_offset=0,
        session=fake_dependencies.session(generation=1),
    )

    assert result.outcome.value == "completed"
    assert result.confirmed_offset == IO_BLOCK_BYTES
    assert fake_dependencies.remote.generation_switch_blocked is True


def test_empty_directory_creates_nested_parents_and_unique_temporary_directory(
    fake_dependencies,
) -> None:
    item = fake_dependencies.item(size=0)
    item = replace(
        item,
        relative_path=PurePosixPath("album/empty"),
        final_path=RemotePath("target/album/empty"),
        temp_path=RemotePath("target/album/.empty.part"),
        source_fingerprint=replace(item.source_fingerprint, kind=SourceKind.EMPTY_DIRECTORY),
    )
    fake_dependencies.local.fingerprint_override = item.source_fingerprint
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())

    result = writer.copy(item, 0, fake_dependencies.session(generation=1))

    assert result.outcome.value == "completed"
    assert "target/album" in fake_dependencies.remote.directories
    assert item.temp_path.value in fake_dependencies.remote.directories
    assert fake_dependencies.repository.checkpoints == []


def test_file_copy_creates_missing_remote_parent_directories(fake_dependencies) -> None:
    item = replace(
        fake_dependencies.item(size=7),
        relative_path=PurePosixPath("album/file.bin"),
        final_path=RemotePath("target/album/file.bin"),
        temp_path=RemotePath("target/album/.file.bin.part"),
    )
    writer = CheckpointWriter(**fake_dependencies.as_kwargs())

    result = writer.copy(item, 0, fake_dependencies.session(generation=1))

    assert result.outcome.value == "completed"
    assert "target/album" in fake_dependencies.remote.directories
