from __future__ import annotations

import errno
import re
import socket

from nasmove.core.errors import TransferErrorCategory, TransferFailure


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
    0xC000006D: "authentication_failed",  # STATUS_LOGON_FAILURE
    0xC0000234: "account_locked",  # STATUS_ACCOUNT_LOCKED_OUT
    0xC0000043: "file_locked",  # STATUS_SHARING_VIOLATION
    0xC000009F: "quota_exceeded",  # STATUS_QUOTA_EXCEEDED
    0xC0000033: "invalid_name",  # STATUS_OBJECT_NAME_INVALID
    0xC00000BB: "unsupported",  # STATUS_NOT_SUPPORTED
    0xC00000C9: "network_name_deleted",  # STATUS_NETWORK_NAME_DELETED
    0xC00000B5: "timeout",  # STATUS_IO_TIMEOUT
}
_STATUS_NAME_CODES = {
    "STATUS_ACCESS_DENIED": "permission_denied",
    "STATUS_OBJECT_NAME_NOT_FOUND": "path_not_found",
    "STATUS_OBJECT_NAME_COLLISION": "target_exists",
    "STATUS_OBJECT_PATH_NOT_FOUND": "path_not_found",
    "STATUS_DISK_FULL": "disk_full",
    "STATUS_LOGON_FAILURE": "authentication_failed",
    "STATUS_ACCOUNT_LOCKED_OUT": "account_locked",
    "STATUS_SHARING_VIOLATION": "file_locked",
    "STATUS_QUOTA_EXCEEDED": "quota_exceeded",
    "STATUS_OBJECT_NAME_INVALID": "invalid_name",
    "STATUS_NOT_SUPPORTED": "unsupported",
    "STATUS_NETWORK_NAME_DELETED": "network_name_deleted",
    "STATUS_IO_TIMEOUT": "timeout",
    "STATUS_CONNECTION_RESET": "connection_reset",
    "STATUS_CONNECTION_DISCONNECTED": "connection_reset",
    "STATUS_NETWORK_SESSION_EXPIRED": "connection_reset",
    "STATUS_USER_SESSION_DELETED": "connection_reset",
}

_NETWORK_CODES = frozenset({"network_name_deleted", "timeout", "connection_reset"})
_CODE_CATEGORIES = {
    "permission_denied": TransferErrorCategory.PERMISSION,
    "path_not_found": TransferErrorCategory.NOT_FOUND,
    "target_exists": TransferErrorCategory.TARGET_EXISTS,
    "disk_full": TransferErrorCategory.DISK_FULL,
    "quota_exceeded": TransferErrorCategory.QUOTA,
    "authentication_failed": TransferErrorCategory.AUTHENTICATION,
    "account_locked": TransferErrorCategory.ACCOUNT_LOCKED,
    "file_locked": TransferErrorCategory.LOCKED,
    "invalid_name": TransferErrorCategory.INVALID_NAME,
    "unsupported": TransferErrorCategory.UNSUPPORTED,
    "network_name_deleted": TransferErrorCategory.NETWORK,
    "timeout": TransferErrorCategory.NETWORK,
    "connection_reset": TransferErrorCategory.NETWORK,
    "dns_failure": TransferErrorCategory.DNS,
}

_RETRYABLE_ERRNOS = frozenset(
    {
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.EPIPE,
        errno.ENETDOWN,
        errno.ENETRESET,
        errno.ENETUNREACH,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ETIMEDOUT,
    }
)


def map_smb_error(error: BaseException) -> TransferFailure:
    """Map an SMB boundary exception to a safe, non-sensitive transfer failure."""
    code = redacted_error_code(error)
    if isinstance(error, socket.gaierror) or code == "dns_failure":
        return TransferFailure(TransferErrorCategory.DNS, "dns_failure", False)
    if code in _CODE_CATEGORIES:
        return TransferFailure(_CODE_CATEGORIES[code], code, code in _NETWORK_CODES)
    if isinstance(error, TimeoutError):
        return TransferFailure(TransferErrorCategory.NETWORK, "timeout", True)
    if isinstance(error, OSError) and error.errno in _RETRYABLE_ERRNOS:
        return TransferFailure(TransferErrorCategory.NETWORK, "connection_reset", True)
    return TransferFailure(TransferErrorCategory.UNKNOWN, code, False)


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

    if isinstance(error, ConnectionError):
        return "connection_reset"

    if isinstance(error, socket.gaierror):
        return "dns_failure"

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
        if error.errno == getattr(errno, "EDQUOT", -1):
            return "quota_exceeded"
        if error.errno == errno.ENOENT:
            return "path_not_found"
        if error.errno == errno.EEXIST:
            return "target_exists"
        if error.errno in {errno.EACCES, errno.EPERM}:
            return "permission_denied"
        if error.errno in {errno.EBUSY, errno.EAGAIN}:
            return "file_locked"
        if error.errno in _RETRYABLE_ERRNOS:
            return "connection_reset"
        return f"os_error_{error.errno}"
    return "unexpected_error"
