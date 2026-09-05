from __future__ import annotations

import os
from pathlib import Path

from nasmove.core.model import ConnectionConfig, ConnectionProfileId, RemotePath
from nasmove.core.ports import RemoteEntry
from nasmove.core.states import ItemState, SourceKind
from nasmove.planning.task_planner import PlannedTask, PlanRequest, TaskPlanner


class LocalGateway:
    def fingerprint(self, path: Path):
        from nasmove.core.model import SourceFingerprint

        info = path.stat()
        return SourceFingerprint(
            device=info.st_dev,
            inode=info.st_ino,
            kind=SourceKind.FILE,
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
        )


class SmbGateway:
    def __init__(self, free_space: int = 1 << 40) -> None:
        self.free_space_value = free_space
        self.batches: list[tuple[object, ...]] = []

    def list_dir(self, path: RemotePath) -> list[RemoteEntry]:
        del path
        return []

    def free_space(self, path: RemotePath) -> int:
        del path
        return self.free_space_value


def config() -> ConnectionConfig:
    return ConnectionConfig(
        profile_id=ConnectionProfileId("profile"),
        display_name="NAS",
        host="nas.local",
        share="archive",
        username="operator",
    )


def test_planner_preserves_top_level_and_skips_links_and_special_files(tmp_path: Path) -> None:
    source = tmp_path / "Source"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "file.txt").write_text("payload")
    os.symlink(source / "nested" / "file.txt", source / "link.txt")
    fifo = source / "pipe"
    os.mkfifo(fifo)

    planned = TaskPlanner(LocalGateway(), SmbGateway()).plan(
        PlanRequest(
            name="copy",
            connection=config(),
            sources=(source,),
            target_root=RemotePath("incoming"),
        )
    )

    assert isinstance(planned, PlannedTask)
    paths = {item.relative_path.as_posix(): item for item in planned.items}
    assert "Source/nested/file.txt" in paths
    assert paths["Source/link.txt"].state is ItemState.SKIPPED
    assert paths["Source/pipe"].state is ItemState.SKIPPED


def test_empty_directory_is_planned() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "Empty"
        source.mkdir()
        planned = TaskPlanner(LocalGateway(), SmbGateway()).plan(
            PlanRequest("copy", config(), (source,), RemotePath("incoming"))
        )

    assert len(planned.items) == 1
    assert planned.items[0].source_fingerprint.kind is SourceKind.EMPTY_DIRECTORY
    assert planned.items[0].state is ItemState.PLANNED


def test_planner_batches_every_thousand_items(tmp_path: Path) -> None:
    source = tmp_path / "bulk"
    source.mkdir()
    for index in range(1001):
        (source / f"{index}.txt").write_text("x")
    sink: list[tuple[object, ...]] = []

    TaskPlanner(LocalGateway(), SmbGateway(), batch_sink=sink.append).plan(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    assert [len(batch) for batch in sink] == [1000, 1]


def test_space_reserve_is_one_gib_or_five_percent(tmp_path: Path) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"x" * 10)
    gateway = SmbGateway(free_space=(1 << 30) + 10)

    planned = TaskPlanner(LocalGateway(), gateway).plan(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    assert planned.safety_margin == 1 << 30
    assert planned.required_space == (1 << 30) + 10
