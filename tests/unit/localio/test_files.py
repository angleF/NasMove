from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

import pytest

from nasmove.core.states import SourceKind
from nasmove.localio.files import PosixLocalFileGateway


def test_fingerprint_changes_when_content_changes(tmp_path: Path) -> None:
    path = tmp_path / "source.bin"
    path.write_bytes(b"first")
    gateway = PosixLocalFileGateway()

    before = gateway.fingerprint(path)
    path.write_bytes(b"second")
    after = gateway.fingerprint(path)

    assert before != after
    assert before.kind is SourceKind.FILE
    assert after.size == len(b"second")


def test_fingerprint_supports_empty_directory(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.mkdir()

    fingerprint = PosixLocalFileGateway().fingerprint(path)

    assert fingerprint.kind is SourceKind.EMPTY_DIRECTORY
    assert fingerprint.size == 0


def test_fingerprint_rejects_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"payload")
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    with pytest.raises(OSError):
        PosixLocalFileGateway().fingerprint(link)


def test_open_read_is_binary_and_read_only(tmp_path: Path) -> None:
    path = tmp_path / "source.bin"
    path.write_bytes(b"payload")

    with PosixLocalFileGateway().open_read(path) as stream:
        assert stream.read() == b"payload"
        assert "b" in stream.mode
        with pytest.raises((OSError, AttributeError)):
            stream.write(b"not allowed")  # type: ignore[attr-defined]


def test_open_read_rejects_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"payload")
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    with pytest.raises(OSError), PosixLocalFileGateway().open_read(link):
        pass


@pytest.mark.parametrize("operation", ["fingerprint", "open_read", "remove_file"])
def test_operations_reject_symbolic_link_ancestor(tmp_path: Path, operation: str) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    target = real_parent / "source.bin"
    target.write_bytes(b"payload")
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    escaped_path = alias / "source.bin"
    gateway = PosixLocalFileGateway()

    with pytest.raises(OSError):
        if operation == "fingerprint":
            gateway.fingerprint(escaped_path)
        elif operation == "open_read":
            gateway.open_read(escaped_path)
        else:
            gateway.remove_file(escaped_path)

    assert target.exists()


def test_open_read_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "source.fifo"
    os.mkfifo(path)

    with pytest.raises(OSError):
        PosixLocalFileGateway().open_read(path)


def test_remove_file_uses_anchored_parent_fd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "source.bin"
    path.write_bytes(b"payload")
    calls: list[int | None] = []
    original_unlink = os.unlink

    def recording_unlink(path_arg: os.PathLike[str] | str, *, dir_fd: int | None = None) -> None:
        calls.append(dir_fd)
        original_unlink(path_arg, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", recording_unlink)
    PosixLocalFileGateway().remove_file(path)

    assert calls and calls[0] is not None


def test_remove_file_rejects_changed_expected_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "source.bin"
    path.write_bytes(b"before")
    gateway = PosixLocalFileGateway()
    expected = gateway.fingerprint(path)
    path.write_bytes(b"after with a different size")

    with pytest.raises(OSError) as error:
        gateway.remove_file(path, expected_fingerprint=expected)

    assert error.value.errno == errno.EAGAIN
    assert path.read_bytes() == b"after with a different size"


def test_remove_file_rejects_expected_type_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"payload")
    gateway = PosixLocalFileGateway()
    expected = gateway.fingerprint(path)
    path.unlink()
    path.mkdir()

    with pytest.raises(OSError):
        gateway.remove_file(path, expected_fingerprint=expected)

    assert path.is_dir()


def test_remove_empty_dir_rejects_changed_expected_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.mkdir()
    gateway = PosixLocalFileGateway()
    expected = gateway.fingerprint(path)
    (path / "new-child").write_bytes(b"payload")

    with pytest.raises(OSError):
        gateway.remove_empty_dir(path, expected_fingerprint=expected)

    assert path.exists()


def test_deletion_uses_parent_flock_and_dir_fd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    file_path = tmp_path / "source.bin"
    file_path.write_bytes(b"payload")
    directory = tmp_path / "empty"
    directory.mkdir()
    flock_calls: list[tuple[int, int]] = []
    unlink_fds: list[int | None] = []
    rmdir_fds: list[int | None] = []
    original_unlink = os.unlink
    original_rmdir = os.rmdir
    original_flock = fcntl.flock

    def recording_flock(fd: int, operation: int) -> None:
        flock_calls.append((fd, operation))
        original_flock(fd, operation)

    def recording_unlink(path_arg: os.PathLike[str] | str, *, dir_fd: int | None = None) -> None:
        unlink_fds.append(dir_fd)
        original_unlink(path_arg, dir_fd=dir_fd)

    def recording_rmdir(path_arg: os.PathLike[str] | str, *, dir_fd: int | None = None) -> None:
        rmdir_fds.append(dir_fd)
        original_rmdir(path_arg, dir_fd=dir_fd)

    monkeypatch.setattr(fcntl, "flock", recording_flock)
    monkeypatch.setattr(os, "unlink", recording_unlink)
    monkeypatch.setattr(os, "rmdir", recording_rmdir)
    gateway = PosixLocalFileGateway()

    gateway.remove_file(file_path)
    gateway.remove_empty_dir(directory)

    assert [operation for _, operation in flock_calls] == [fcntl.LOCK_EX, fcntl.LOCK_UN] * 2
    assert unlink_fds and unlink_fds[0] is not None
    assert rmdir_fds and rmdir_fds[0] is not None


def test_remove_file_and_empty_directory_are_separate(tmp_path: Path) -> None:
    file_path = tmp_path / "source.bin"
    file_path.write_bytes(b"payload")
    directory = tmp_path / "empty"
    directory.mkdir()
    gateway = PosixLocalFileGateway()

    gateway.remove_file(file_path)
    gateway.remove_empty_dir(directory)

    assert not file_path.exists()
    assert not directory.exists()


def test_remove_file_does_not_remove_directory(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()

    with pytest.raises(OSError):
        PosixLocalFileGateway().remove_file(directory)

    assert directory.is_dir()


def test_remove_empty_dir_rejects_non_empty_directory(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "child").write_bytes(b"payload")

    with pytest.raises(OSError):
        PosixLocalFileGateway().remove_empty_dir(directory)

    assert directory.exists()


def test_gateway_uses_no_follow_stat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "source.bin"
    path.write_bytes(b"payload")
    calls: list[bool] = []
    original_stat = os.stat

    def recording_stat(
        path_arg: os.PathLike[str] | str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        calls.append(follow_symlinks)
        return original_stat(path_arg, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", recording_stat)
    PosixLocalFileGateway().fingerprint(path)

    assert calls == [False]
