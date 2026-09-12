from __future__ import annotations

import errno
import socket

import pytest
from smbprotocol.exceptions import (
    SMBAuthenticationError,
    SMBConnectionClosed,
    SMBUnsupportedFeature,
)
from smbprotocol.header import NtStatus

from nasmove.core.errors import SourceFileMissingError, TransferErrorCategory
from nasmove.smb.error_mapping import map_smb_error, redacted_error_code
from tests.fixtures.fake_smb import FakeNtStatusError


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        (FakeNtStatusError("STATUS_ACCESS_DENIED"), TransferErrorCategory.PERMISSION, False),
        (FakeNtStatusError("STATUS_LOGON_FAILURE"), TransferErrorCategory.AUTHENTICATION, False),
        (FakeNtStatusError("STATUS_ACCOUNT_LOCKED_OUT"), TransferErrorCategory.ACCOUNT_LOCKED, False),
        (FakeNtStatusError("STATUS_DISK_FULL"), TransferErrorCategory.DISK_FULL, False),
        (FakeNtStatusError("STATUS_QUOTA_EXCEEDED"), TransferErrorCategory.QUOTA, False),
        (FakeNtStatusError("STATUS_OBJECT_NAME_NOT_FOUND"), TransferErrorCategory.NOT_FOUND, False),
        (FakeNtStatusError("STATUS_OBJECT_NAME_COLLISION"), TransferErrorCategory.TARGET_EXISTS, False),
        (FakeNtStatusError("STATUS_SHARING_VIOLATION"), TransferErrorCategory.LOCKED, False),
        (FakeNtStatusError("STATUS_OBJECT_NAME_INVALID"), TransferErrorCategory.INVALID_NAME, False),
        (FakeNtStatusError("STATUS_NOT_SUPPORTED"), TransferErrorCategory.UNSUPPORTED, False),
    ],
)
def test_nt_status_errors_are_classified_without_retry(
    error: BaseException,
    category: TransferErrorCategory,
    retryable: bool,
) -> None:
    failure = map_smb_error(error)

    assert failure.category is category
    assert failure.retryable is retryable


def test_access_denied_is_not_retryable() -> None:
    failure = map_smb_error(FakeNtStatusError("STATUS_ACCESS_DENIED"))
    assert failure.category is TransferErrorCategory.PERMISSION
    assert failure.retryable is False


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("connection reset"),
        TimeoutError("timed out"),
        OSError(errno.ECONNRESET, "connection reset"),
        OSError(errno.ECONNABORTED, "connection aborted"),
        OSError(errno.EHOSTUNREACH, "host unreachable"),
    ],
)
def test_transient_network_errors_are_retryable(error: BaseException) -> None:
    failure = map_smb_error(error)

    assert failure.category is TransferErrorCategory.NETWORK
    assert failure.retryable is True


def test_dns_failure_is_not_treated_as_transient_transfer_retry() -> None:
    failure = map_smb_error(socket.gaierror(socket.EAI_NONAME, "name or service not known"))

    assert failure.category is TransferErrorCategory.DNS
    assert failure.retryable is False


def test_real_ntstatus_int_is_supported() -> None:
    failure = map_smb_error(FakeNtStatusError(NtStatus.STATUS_DISK_FULL))

    assert failure.category is TransferErrorCategory.DISK_FULL
    assert failure.code == "disk_full"


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        (SMBAuthenticationError("secret must not be logged"), TransferErrorCategory.AUTHENTICATION, False),
        (SMBConnectionClosed("server detail"), TransferErrorCategory.NETWORK, True),
        (SMBUnsupportedFeature("dialect detail"), TransferErrorCategory.UNSUPPORTED, False),
    ],
)
def test_real_smb_exception_classes_are_classified(
    error: BaseException,
    category: TransferErrorCategory,
    retryable: bool,
) -> None:
    failure = map_smb_error(error)

    assert failure.category is category
    assert failure.retryable is retryable


def test_temporary_dns_failure_is_retryable() -> None:
    failure = map_smb_error(socket.gaierror(socket.EAI_AGAIN, "temporary resolver failure"))

    assert failure.category is TransferErrorCategory.DNS
    assert failure.retryable is True


def test_local_missing_source_has_its_own_code_distinct_from_remote_path() -> None:
    assert redacted_error_code(SourceFileMissingError(errno.ENOENT, "source file is missing")) == (
        "source_not_found"
    )
    # A remote-object miss keeps its own meaning, whether it arrives as a plain
    # FileNotFoundError or as an NTSTATUS.
    assert redacted_error_code(FileNotFoundError(errno.ENOENT, "remote path")) == "path_not_found"
    assert redacted_error_code(FakeNtStatusError("STATUS_OBJECT_NAME_NOT_FOUND")) == "path_not_found"


def test_local_missing_source_maps_to_a_non_retryable_not_found_failure() -> None:
    failure = map_smb_error(SourceFileMissingError(errno.ENOENT, "source file is missing"))

    assert failure.category is TransferErrorCategory.NOT_FOUND
    assert failure.code == "source_not_found"
    assert failure.retryable is False
