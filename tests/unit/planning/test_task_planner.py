from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path

import pytest

import nasmove.planning.task_planner as planner_module
from nasmove.core.errors import (
    ConflictResolutionRequired,
    DomainValidationError,
    PreflightCancelled,
)
from nasmove.core.model import ConnectionConfig, ConnectionProfileId, RemotePath
from nasmove.core.ports import RemoteEntry, RemoteStat
from nasmove.core.states import ConflictPolicy, ItemState, SourceKind
from nasmove.planning.task_planner import (
    PlannedTask,
    PlanRequest,
    PreflightCancellation,
    TaskPlanner,
)


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
    def __init__(self, free_space: int = 1 << 40, events: list[str] | None = None) -> None:
        self.free_space_value = free_space
        self.list_calls: list[str] = []
        self.events = events

    def list_dir(self, path: RemotePath) -> list[RemoteEntry]:
        self.list_calls.append(path.value)
        return []

    def free_space(self, path: RemotePath) -> int:
        del path
        if self.events is not None:
            self.events.append("free_space")
        return self.free_space_value


class AtomicRepository:
    def __init__(
        self,
        *,
        fail: bool = False,
        events: list[str] | None = None,
        sample_limit: int = 20,
    ) -> None:
        self.fail = fail
        self.calls: list[str] = []
        self.sample_limit = sample_limit
        self.samples: tuple[object, ...] = ()
        self.item_count = 0
        self.max_batch = 0
        self.events = events

    def create_task(self, task: object, items: object) -> None:
        del task
        self.calls.append("create_task")
        if self.events is not None:
            self.events.append("create_task")
        pending: list[object] = []
        consumed = 0
        current_batch = 0
        try:
            for item in items:
                consumed += 1
                current_batch += 1
                self.max_batch = max(self.max_batch, current_batch)
                if len(pending) < self.sample_limit:
                    pending.append(item)
                if current_batch == 1000:
                    current_batch = 0
                if self.fail:
                    raise RuntimeError("simulated transaction failure")
        except Exception:
            pending.clear()
            self.item_count = 0
            self.samples = ()
            raise
        self.item_count = consumed
        self.samples = tuple(pending)

    @property
    def persisted(self) -> tuple[object, ...]:
        return self.samples


class EarlyStopRepository:
    def __init__(self) -> None:
        self.calls = 0

    def create_task(self, task: object, items: object) -> None:
        del task, items
        self.calls += 1


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

    repository = AtomicRepository()
    planned = TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
        PlanRequest(
            name="copy",
            connection=config(),
            sources=(source,),
            target_root=RemotePath("incoming"),
        )
    )

    assert isinstance(planned, PlannedTask)
    paths = {item.relative_path.as_posix(): item for item in repository.persisted}
    assert "Source/nested/file.txt" in paths
    assert paths["Source/link.txt"].state is ItemState.SKIPPED
    assert paths["Source/pipe"].state is ItemState.SKIPPED


def test_empty_directory_is_planned(tmp_path: Path) -> None:
    source = tmp_path / "Empty"
    source.mkdir()
    repository = AtomicRepository()
    planned = TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    assert planned.item_count == 1
    item = repository.persisted[0]
    assert item.source_fingerprint.kind is SourceKind.EMPTY_DIRECTORY
    assert item.state is ItemState.PLANNED


def test_planner_batches_every_thousand_items(tmp_path: Path) -> None:
    source = tmp_path / "bulk"
    source.mkdir()
    for index in range(1001):
        (source / f"{index}.txt").write_text("x")
    repository = AtomicRepository()

    TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    assert repository.item_count == 1001
    assert repository.max_batch <= 1000


def test_space_reserve_is_one_gib_or_five_percent(tmp_path: Path) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"x" * 10)
    gateway = SmbGateway(free_space=(1 << 30) + 10)

    planned = TaskPlanner(LocalGateway(), gateway).plan(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    assert planned.safety_margin == 1 << 30
    assert planned.required_space == (1 << 30) + 10


def test_space_check_precedes_persistence(tmp_path: Path) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"x")
    repository = AtomicRepository()

    with pytest.raises(DomainValidationError):
        TaskPlanner(LocalGateway(), SmbGateway(free_space=0), repository).plan(
            PlanRequest("copy", config(), (source,), RemotePath("incoming"))
        )

    assert repository.calls == []
    assert repository.persisted == ()


def test_preflight_does_not_persist_until_explicit_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"payload")
    repository = AtomicRepository()
    planner = TaskPlanner(LocalGateway(), SmbGateway(), repository)

    session = planner.preflight(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    assert repository.calls == []
    assert session.summary.item_count == 1
    assert session.spool_path.exists()

    planned = planner.confirm_preflight(session)

    assert repository.calls == ["create_task"]
    assert planned.item_count == 1
    assert session.closed is True
    assert not session.spool_path.exists()


def test_canceling_preflight_discards_spool_without_creating_task(tmp_path: Path) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"payload")
    repository = AtomicRepository()
    planner = TaskPlanner(LocalGateway(), SmbGateway(), repository)
    session = planner.preflight(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    planner.cancel_preflight(session)

    assert session.closed is True
    assert not session.spool_path.exists()
    assert repository.calls == []
    with pytest.raises(PreflightCancelled):
        planner.confirm_preflight(session)


def test_preflight_cancellation_is_observed_during_source_scan(tmp_path: Path) -> None:
    source = tmp_path / "bulk"
    source.mkdir()
    for index in range(10):
        (source / f"{index}.txt").write_text("payload")
    repository = AtomicRepository()
    planner = TaskPlanner(LocalGateway(), SmbGateway(), repository)
    cancellation = PreflightCancellation()
    progress = []

    def cancel_after_first_item(snapshot) -> None:
        progress.append(snapshot)
        cancellation.cancel()

    with pytest.raises(PreflightCancelled):
        planner.preflight(
            PlanRequest("copy", config(), (source,), RemotePath("incoming")),
            cancellation=cancellation,
            on_progress=cancel_after_first_item,
        )

    assert len(progress) == 1
    assert progress[0].item_count == 1
    assert repository.calls == []


def test_preflight_resolves_conflicts_before_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "same.txt"
    source.write_text("payload")

    class ConflictGateway(SmbGateway):
        def list_dir(self, path: RemotePath) -> list[RemoteEntry]:
            self.list_calls.append(path.value)
            return [RemoteEntry("same.txt", False, 7)] if path.value == "incoming" else []

    gateway = ConflictGateway()
    repository = AtomicRepository()
    planner = TaskPlanner(LocalGateway(), gateway, repository)

    session = planner.preflight(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )
    calls_after_preflight = tuple(gateway.list_calls)

    assert session.summary.conflict_count == 1
    planner.confirm_preflight(session)
    assert tuple(gateway.list_calls) == calls_after_preflight
    assert repository.persisted[0].final_path.value == "incoming/same (1).txt"


@pytest.mark.parametrize(
    ("policy", "expected_path", "expected_state"),
    (
        (ConflictPolicy.KEEP_BOTH, "incoming/same (1).txt", ItemState.PLANNED),
        (ConflictPolicy.OVERWRITE, "incoming/same.txt", ItemState.PLANNED),
        (ConflictPolicy.SKIP, "incoming/same.txt", ItemState.SKIPPED),
    ),
)
def test_preflight_applies_deterministic_conflict_policy(
    tmp_path: Path,
    policy: ConflictPolicy,
    expected_path: str,
    expected_state: ItemState,
) -> None:
    source = tmp_path / "same.txt"
    source.write_text("payload")

    class ConflictGateway(SmbGateway):
        def list_dir(self, path: RemotePath) -> list[RemoteEntry]:
            self.list_calls.append(path.value)
            return [RemoteEntry("same.txt", False, 7)]

    repository = AtomicRepository()
    planner = TaskPlanner(LocalGateway(), ConflictGateway(), repository)

    planned = planner.plan(
        PlanRequest(
            "copy",
            config(),
            (source,),
            RemotePath("incoming"),
            conflict_policy=policy,
        )
    )

    assert planned.conflict_count == 1
    assert repository.persisted[0].final_path.value == expected_path
    assert repository.persisted[0].state is expected_state
    expected_files = 0 if expected_state is ItemState.SKIPPED else 1
    assert planned.task.total_files == expected_files
    assert planned.task.total_bytes == (0 if expected_files == 0 else len("payload"))


@pytest.mark.parametrize(
    ("remote_age_delta", "expected_state"),
    ((-1, ItemState.PLANNED), (1, ItemState.SKIPPED)),
)
def test_overwrite_if_newer_compares_source_and_remote_mtime(
    tmp_path: Path, remote_age_delta: int, expected_state: ItemState
) -> None:
    source = tmp_path / "same.txt"
    source.write_text("payload")

    class ConflictGateway(SmbGateway):
        def list_dir(self, path: RemotePath) -> list[RemoteEntry]:
            self.list_calls.append(path.value)
            return [RemoteEntry("same.txt", False, 7)]

        def stat(self, path: RemotePath) -> RemoteStat:
            assert path.value == "incoming/same.txt"
            return RemoteStat(
                size=7,
                is_directory=False,
                modified_ns=source.stat().st_mtime_ns + remote_age_delta,
                file_id="remote",
            )

    repository = AtomicRepository()
    TaskPlanner(LocalGateway(), ConflictGateway(), repository).plan(
        PlanRequest(
            "copy",
            config(),
            (source,),
            RemotePath("incoming"),
            conflict_policy=ConflictPolicy.OVERWRITE_IF_NEWER,
        )
    )

    assert repository.persisted[0].state is expected_state


def test_ask_policy_requires_an_explicit_conflict_decision(tmp_path: Path) -> None:
    source = tmp_path / "same.txt"
    source.write_text("payload")

    class ConflictGateway(SmbGateway):
        def list_dir(self, path: RemotePath) -> list[RemoteEntry]:
            return [RemoteEntry("same.txt", False, 7)]

    with pytest.raises(ConflictResolutionRequired) as raised:
        TaskPlanner(LocalGateway(), ConflictGateway()).preflight(
            PlanRequest(
                "copy",
                config(),
                (source,),
                RemotePath("incoming"),
                conflict_policy=ConflictPolicy.ASK,
            )
        )

    assert raised.value.remote_path == RemotePath("incoming/same.txt")


def test_successful_space_check_precedes_persistence(tmp_path: Path) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"x")
    events: list[str] = []
    repository = AtomicRepository(events=events)
    gateway = SmbGateway(events=events)

    TaskPlanner(LocalGateway(), gateway, repository).plan(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    assert events[:2] == ["free_space", "create_task"]


def test_repository_failure_rolls_back_plan(tmp_path: Path) -> None:
    source = tmp_path / "file.txt"
    source.write_text("payload")
    repository = AtomicRepository(fail=True)
    with pytest.raises(RuntimeError):
        TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
            PlanRequest("copy", config(), (source,), RemotePath("incoming"))
        )
    assert repository.persisted == ()


def test_repository_must_consume_items_completely(tmp_path: Path) -> None:
    source = tmp_path / "file.txt"
    source.write_text("payload")
    repository = EarlyStopRepository()

    with pytest.raises(DomainValidationError):
        TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
            PlanRequest("copy", config(), (source,), RemotePath("incoming"))
        )

    assert repository.calls == 1


def test_sources_must_not_overlap(tmp_path: Path) -> None:
    directory = tmp_path / "source"
    directory.mkdir()
    child = directory / "child.txt"
    child.write_text("payload")
    subdirectory = directory / "subdirectory"
    subdirectory.mkdir()

    for sources in ((directory, directory), (directory, child), (directory, subdirectory)):
        repository = AtomicRepository()
        with pytest.raises(DomainValidationError):
            TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
                PlanRequest("copy", config(), sources, RemotePath("incoming"))
            )
        assert repository.calls == []


def test_remote_directory_is_listed_once(tmp_path: Path) -> None:
    source = tmp_path / "Source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "file.txt").write_text("payload")
    gateway = SmbGateway()
    repository = AtomicRepository()
    TaskPlanner(LocalGateway(), gateway, repository).plan(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    assert gateway.list_calls.count("incoming") == 1
    assert gateway.list_calls.count("incoming/Source") == 1


def test_temp_names_are_unique_across_tasks_with_same_source_name(tmp_path: Path) -> None:
    source = tmp_path / "same-name.txt"
    source.write_text("payload")
    first_repository = AtomicRepository()
    second_repository = AtomicRepository()

    first = TaskPlanner(LocalGateway(), SmbGateway(), first_repository).plan(
        PlanRequest("first", config(), (source,), RemotePath("incoming"))
    )
    second = TaskPlanner(LocalGateway(), SmbGateway(), second_repository).plan(
        PlanRequest("second", config(), (source,), RemotePath("incoming"))
    )

    first_item = first_repository.persisted[0]
    second_item = second_repository.persisted[0]
    assert first_item.temp_path.value == f"incoming/.nasmove-{first_item.id}.part"
    assert second_item.temp_path.value == f"incoming/.nasmove-{second_item.id}.part"
    assert first_item.temp_path != second_item.temp_path
    assert first.task.id != second.task.id


def test_directory_replaced_by_symlink_is_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "Source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    repository = AtomicRepository()
    real_open = planner_module.os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if dir_fd is not None and path == "nested" and not replaced:
            shutil.rmtree(nested)
            os.symlink(outside, nested)
            replaced = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(planner_module.os, "open", replacing_open)
    TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    items = repository.persisted
    assert len(items) == 1
    assert items[0].relative_path.as_posix() == "Source/nested"
    assert items[0].state is ItemState.SKIPPED


def test_explicit_source_rejects_symlink_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    source = real_parent / "file.txt"
    source.write_text("payload")
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(DomainValidationError):
        TaskPlanner(LocalGateway(), SmbGateway()).plan(
            PlanRequest("copy", config(), (alias_parent / "file.txt",), RemotePath("incoming"))
        )


@pytest.mark.skipif(not Path("/var").is_symlink(), reason="macOS /var alias is not present")
def test_planner_accepts_macos_private_var_alias(tmp_path: Path) -> None:
    real_source = tmp_path / "source.txt"
    real_source.write_text("payload")
    source = Path("/var") / real_source.resolve().relative_to("/private/var")
    try:
        repository = AtomicRepository()
        TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
            PlanRequest("copy", config(), (source,), RemotePath("incoming"))
        )
        assert repository.item_count == 1
    finally:
        real_source.unlink(missing_ok=True)


def test_hard_link_alias_sources_are_rejected(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("payload")
    second.hardlink_to(first)

    with pytest.raises(DomainValidationError):
        TaskPlanner(LocalGateway(), SmbGateway()).plan(
            PlanRequest("copy", config(), (first, second), RemotePath("incoming"))
        )


def test_file_replaced_by_symlink_after_stat_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "Source"
    source.mkdir()
    file_path = source / "file.txt"
    file_path.write_text("payload")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    repository = AtomicRepository()
    real_open = planner_module.os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if dir_fd is not None and path == "file.txt" and not replaced:
            file_path.unlink()
            file_path.symlink_to(outside)
            replaced = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(planner_module.os, "open", replacing_open)
    TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    items = repository.persisted
    assert len(items) == 1
    assert items[0].relative_path.as_posix() == "Source/file.txt"
    assert items[0].state is ItemState.SKIPPED


def test_planner_scans_each_source_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "file.txt"
    source.write_text("payload")
    planner = TaskPlanner(LocalGateway(), SmbGateway(), AtomicRepository())
    real_scan = planner._scan
    scan_count = 0

    def counting_scan(root: Path):
        nonlocal scan_count
        scan_count += 1
        yield from real_scan(root)

    monkeypatch.setattr(planner, "_scan", counting_scan)
    planner.plan(PlanRequest("copy", config(), (source,), RemotePath("incoming")))

    assert scan_count == 1


def test_explicit_top_level_symlink_is_persisted_as_skipped(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("outside")
    source = tmp_path / "link.txt"
    source.symlink_to(target)
    repository = AtomicRepository()

    TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
        PlanRequest("copy", config(), (source,), RemotePath("incoming"))
    )

    items = repository.persisted
    assert len(items) == 1
    assert items[0].relative_path.as_posix() == "link.txt"
    assert items[0].state is ItemState.SKIPPED


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_explicit_top_level_special_source_never_opens_final_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    source = tmp_path / f"{kind}.special"
    listener: socket.socket | None = None
    if kind == "fifo":
        os.mkfifo(source)
    else:
        source = Path("/private/tmp") / "nasmove-planning-test.sock"
        source.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(source))
    repository = AtomicRepository()
    real_open = planner_module.os.open

    def reject_final_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and path == source.name:
            raise AssertionError("special final components must be classified by lstat")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(planner_module.os, "open", reject_final_open)
    try:
        TaskPlanner(LocalGateway(), SmbGateway(), repository).plan(
            PlanRequest("copy", config(), (source,), RemotePath("incoming"))
        )
    finally:
        if listener is not None:
            listener.close()
            source.unlink(missing_ok=True)

    items = repository.persisted
    assert len(items) == 1
    assert items[0].relative_path.as_posix() == source.name
    assert items[0].state is ItemState.SKIPPED
