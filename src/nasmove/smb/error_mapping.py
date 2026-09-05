from __future__ import annotations

import errno
import re


class TargetExistsError(FileExistsError):
    """Raised when an exclusive SMB rename would replace an existing target."""


class RenameOutcomeUnknownError(OSError):
    """Raised when the server may have renamed a file but its response was lost."""


class StaleSmbHandleError(OSError):
    """Raised when a handle belongs to an invalidated SMB session."""


class UnsupportedSmbDialectError(OSError):
    """Raised when negotiation does not meet the configured SMB2/SMB3 floor."""


_SAFE_STATUS = re.compile(r"STATUS_[A-Z0-9_]+")
_NTSTATUS_CODES = {
    0xC0000022: "permission_denied",  # STATUS_ACCESS_DENIED
    0xC0000034: "path_not_found",  # STATUS_OBJECT_NAME_NOT_FOUND
    0xC0000035: "target_exists",  # STATUS_OBJECT_NAME_COLLISION
    0xC000003A: "path_not_found",  # STATUS_OBJECT_PATH_NOT_FOUND
    0xC000007F: "disk_full",  # STATUS_DISK_FULL
}
_STATUS_NAME_CODES = {
    "STATUS_ACCESS_DENIED": "permission_denied",
    "STATUS_OBJECT_NAME_NOT_FOUND": "path_not_found",
    "STATUS_OBJECT_NAME_COLLISION": "target_exists",
    "STATUS_OBJECT_PATH_NOT_FOUND": "path_not_found",
    "STATUS_DISK_FULL": "disk_full",
}


def redacted_error_code(error: BaseException) -> str:
    if isinstance(error, TargetExistsError | FileExistsError):
        return "target_exists"
    if isinstance(error, RenameOutcomeUnknownError):
        return "rename_outcome_unknown"
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

    ntstatus = getattr(error, "ntstatus", None)
    if isinstance(ntstatus, int):
        return _NTSTATUS_CODES.get(ntstatus, "smb_error")
    status = getattr(error, "status", None)
    if isinstance(status, str) and _SAFE_STATUS.fullmatch(status):
        return _STATUS_NAME_CODES.get(status, "smb_error")
    if isinstance(status, int):
        return _NTSTATUS_CODES.get(status, "smb_error")
    if isinstance(error, OSError) and error.errno is not None:
        if error.errno == errno.ENOSPC:
            return "disk_full"
        return f"os_error_{error.errno}"
    return "unexpected_error"
