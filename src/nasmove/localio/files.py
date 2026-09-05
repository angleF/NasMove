from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import BinaryIO

from nasmove.core.model import SourceFingerprint
from nasmove.core.states import SourceKind


class PosixLocalFileGateway:
    """Perform the local file operations used by the transfer engine.

    All metadata probes avoid following symbolic links.  Read handles are
    opened with ``O_NOFOLLOW`` as well, so a source cannot be changed to a
    link between the probe and the open operation.
    """

    def fingerprint(self, path: Path) -> SourceFingerprint:
        info = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise OSError(errno.ELOOP, "symbolic links are not supported", path)
        if stat.S_ISREG(info.st_mode):
            kind = SourceKind.FILE
            size = info.st_size
        elif stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                try:
                    next(entries)
                except StopIteration:
                    pass
                else:
                    raise OSError(errno.ENOTEMPTY, "directory is not empty", path)
            kind = SourceKind.EMPTY_DIRECTORY
            size = 0
        else:
            raise OSError(errno.EINVAL, "source is not a regular file or empty directory", path)
        return SourceFingerprint(
            device=info.st_dev,
            inode=info.st_ino,
            kind=kind,
            size=size,
            mtime_ns=info.st_mtime_ns,
        )

    def open_read(self, path: Path) -> BinaryIO:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(errno.EISDIR if stat.S_ISDIR(info.st_mode) else errno.EINVAL,
                              "source is not a regular file", path)
            return os.fdopen(fd, "rb", closefd=True)
        except BaseException:
            os.close(fd)
            raise

    def remove_file(self, path: Path) -> None:
        info = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise OSError(errno.ELOOP, "symbolic links are not supported", path)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EISDIR if stat.S_ISDIR(info.st_mode) else errno.EINVAL,
                          "source is not a regular file", path)
        os.unlink(path)

    def remove_empty_dir(self, path: Path) -> None:
        info = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise OSError(errno.ELOOP, "symbolic links are not supported", path)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(errno.ENOTDIR, "source is not a directory", path)
        os.rmdir(path)
