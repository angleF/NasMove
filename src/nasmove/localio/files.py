from __future__ import annotations

import errno

try:
    import fcntl
except ImportError:  # pragma: no cover - only non-POSIX Python platforms
    fcntl = None  # type: ignore[assignment]
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from PySide6.QtCore import QFile

from nasmove.core.errors import SourceFileMissingError
from nasmove.core.model import SourceFingerprint
from nasmove.core.states import SourceKind


def _qt_move_to_trash(file_name: str) -> tuple[bool, str]:
    return QFile.moveToTrash(file_name)


def _unsafe_path(path: Path, message: str = "unsafe local path") -> OSError:
    return OSError(errno.ELOOP, message, path)


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


@contextmanager
def _anchored_parent(path: Path) -> Iterator[tuple[int, str]]:
    """Yield a parent directory fd and final name without following ancestors."""
    path = Path(path)
    parts = path.parts
    if path.is_absolute():
        parent_fd = os.open(os.sep, _directory_open_flags())
        components = parts[1:]
    else:
        parent_fd = os.open(".", _directory_open_flags())
        components = parts
    if not components or components[-1] in {"", ".", ".."}:
        os.close(parent_fd)
        raise _unsafe_path(path)

    try:
        for component in components[:-1]:
            if component in {"", ".", ".."}:
                raise _unsafe_path(path, "dot segments are not supported")
            next_fd = os.open(component, _directory_open_flags(), dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        yield parent_fd, components[-1]
    finally:
        os.close(parent_fd)


@contextmanager
def _as_missing_source_error(path: Path) -> Iterator[None]:
    """Re-raise a vanished source path as a typed, actionable error."""
    try:
        yield
    except FileNotFoundError as error:
        raise SourceFileMissingError(errno.ENOENT, "source file is missing", path) from error


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _source_fingerprint(info: os.stat_result, kind: SourceKind, size: int) -> SourceFingerprint:
    return SourceFingerprint(
        device=info.st_dev,
        inode=info.st_ino,
        kind=kind,
        size=size,
        mtime_ns=info.st_mtime_ns,
    )


def _fingerprint_for_entry(info: os.stat_result) -> SourceFingerprint | None:
    if stat.S_ISREG(info.st_mode):
        return _source_fingerprint(info, SourceKind.FILE, info.st_size)
    if stat.S_ISDIR(info.st_mode):
        return _source_fingerprint(info, SourceKind.EMPTY_DIRECTORY, 0)
    return None


def _changed(path: Path) -> OSError:
    return OSError(errno.EAGAIN, "source changed before deletion", path)


@contextmanager
def _locked_parent(parent_fd: int) -> Iterator[None]:
    """Serialize deletions made by this application for one parent directory."""
    if fcntl is None:
        yield
        return
    fcntl.flock(parent_fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(parent_fd, fcntl.LOCK_UN)


class PosixLocalFileGateway:
    """Perform the local file operations used by the transfer engine.

    All metadata probes avoid following symbolic links.  Read handles are
    opened with ``O_NOFOLLOW`` as well, so a source cannot be changed to a
    link between the probe and the open operation.
    """

    def fingerprint(self, path: Path) -> SourceFingerprint:
        with _as_missing_source_error(path), _anchored_parent(path) as (parent_fd, name):
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise _unsafe_path(path, "symbolic links are not supported")
            if stat.S_ISREG(info.st_mode):
                return _source_fingerprint(info, SourceKind.FILE, info.st_size)
            if not stat.S_ISDIR(info.st_mode):
                raise OSError(errno.EINVAL, "source is not a regular file or empty directory", path)

            directory_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
            try:
                bound = os.fstat(directory_fd)
                if not _same_entry(info, bound) or not stat.S_ISDIR(bound.st_mode):
                    raise OSError(errno.EAGAIN, "source changed during fingerprint", path)
                if os.listdir(directory_fd):
                    raise OSError(errno.ENOTEMPTY, "directory is not empty", path)
                return _source_fingerprint(bound, SourceKind.EMPTY_DIRECTORY, 0)
            finally:
                os.close(directory_fd)

    def open_read(self, path: Path) -> BinaryIO:
        with _as_missing_source_error(path), _anchored_parent(path) as (parent_fd, name):
            fd = os.open(name, _file_open_flags(), dir_fd=parent_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    error = errno.EISDIR if stat.S_ISDIR(info.st_mode) else errno.EINVAL
                    raise OSError(error, "source is not a regular file", path)
                return os.fdopen(fd, "rb", closefd=True)
            except BaseException:
                os.close(fd)
                raise

    def remove_file(
        self,
        path: Path,
        expected_fingerprint: SourceFingerprint | None = None,
    ) -> None:
        with _anchored_parent(path) as (parent_fd, name), _locked_parent(parent_fd):
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise _unsafe_path(path, "symbolic links are not supported")
            current = _fingerprint_for_entry(info)
            if current is None:
                raise OSError(errno.EINVAL, "source is not a regular file", path)
            if expected_fingerprint is None:
                expected_fingerprint = current
            elif current != expected_fingerprint:
                raise _changed(path)
            if current.kind is not SourceKind.FILE:
                raise OSError(errno.EISDIR, "source is not a regular file", path)

            fd = os.open(name, _file_open_flags(), dir_fd=parent_fd)
            try:
                bound = os.fstat(fd)
                if _fingerprint_for_entry(bound) != expected_fingerprint:
                    raise _changed(path)
                latest = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if _fingerprint_for_entry(latest) != expected_fingerprint:
                    raise _changed(path)
            finally:
                os.close(fd)
            os.unlink(name, dir_fd=parent_fd)

    def remove_empty_dir(
        self,
        path: Path,
        expected_fingerprint: SourceFingerprint | None = None,
    ) -> None:
        with _anchored_parent(path) as (parent_fd, name), _locked_parent(parent_fd):
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise _unsafe_path(path, "symbolic links are not supported")
            current = _fingerprint_for_entry(info)
            if current is None:
                raise OSError(errno.ENOTDIR, "source is not a directory", path)
            if expected_fingerprint is None:
                expected_fingerprint = current
            elif current != expected_fingerprint:
                raise _changed(path)
            if current.kind is not SourceKind.EMPTY_DIRECTORY:
                raise OSError(errno.ENOTDIR, "source is not a directory", path)

            directory_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
            try:
                bound = os.fstat(directory_fd)
                if _fingerprint_for_entry(bound) != expected_fingerprint:
                    raise _changed(path)
                if os.listdir(directory_fd):
                    raise OSError(errno.ENOTEMPTY, "directory is not empty", path)
                latest = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if _fingerprint_for_entry(latest) != expected_fingerprint:
                    raise _changed(path)
            finally:
                os.close(directory_fd)
            os.rmdir(name, dir_fd=parent_fd)

    def move_to_trash(
        self,
        path: Path,
        expected_fingerprint: SourceFingerprint | None = None,
    ) -> None:
        """Move an unchanged regular file or empty directory to the system Trash."""
        with _anchored_parent(path) as (parent_fd, name), _locked_parent(parent_fd):
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise _unsafe_path(path, "symbolic links are not supported")
            current = _fingerprint_for_entry(info)
            if current is None:
                raise OSError(errno.EINVAL, "source is not a regular file or directory", path)
            if expected_fingerprint is not None and current != expected_fingerprint:
                raise _changed(path)
            expected = current if expected_fingerprint is None else expected_fingerprint

            if current.kind is SourceKind.FILE:
                entry_fd = os.open(name, _file_open_flags(), dir_fd=parent_fd)
            else:
                entry_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
            try:
                bound = os.fstat(entry_fd)
                if _fingerprint_for_entry(bound) != expected:
                    raise _changed(path)
                if current.kind is SourceKind.EMPTY_DIRECTORY and os.listdir(entry_fd):
                    raise OSError(errno.ENOTEMPTY, "directory is not empty", path)
                latest = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if _fingerprint_for_entry(latest) != expected:
                    raise _changed(path)
            finally:
                os.close(entry_fd)

            moved, _trashed_path = _qt_move_to_trash(str(path))
            if not moved:
                raise OSError(errno.EIO, "system trash rejected source", path)
