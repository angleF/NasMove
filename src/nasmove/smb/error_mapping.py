from __future__ import annotations

import errno
import re


class TargetExistsError(FileExistsError):
    """Raised when an exclusive SMB rename would replace an existing target."""


class StaleSmbHandleError(OSError):
    """Raised when a handle belongs to an invalidated SMB session."""


class UnsupportedSmbDialectError(OSError):
    """Raised when negotiation does not meet the configured SMB2/SMB3 floor."""


_SAFE_STATUS = re.compile(r"STATUS_[A-Z0-9_]+")


def redacted_error_code(error: BaseException) -> str:
    if isinstance(error, TargetExistsError | FileExistsError):
        return "target_exists"
    if isinstance(error, StaleSmbHandleError):
        return "stale_handle"
    if isinstance(error, UnsupportedSmbDialectError):
        return "unsupported_dialect"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, FileNotFoundError):
        return "path_not_found"
    if isinstance(error, TimeoutError):
        return "timeout"

    status = getattr(error, "status", None)
    if isinstance(status, str) and _SAFE_STATUS.fullmatch(status):
        return status.lower()
    if isinstance(status, int):
        return f"ntstatus_{status:#010x}"
    if isinstance(error, OSError) and error.errno is not None:
        if error.errno == errno.ENOSPC:
            return "disk_full"
        return f"os_error_{error.errno}"
    return "unexpected_error"
