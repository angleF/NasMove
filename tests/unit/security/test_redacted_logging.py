from __future__ import annotations

import logging
from pathlib import Path

from nasmove.security.redacted_logging import RedactingFilter, configure_logging


def _message(msg: object, args: object = ()) -> str:
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, msg, args, None)
    RedactingFilter().filter(record)
    return record.getMessage()


def test_password_and_unc_credentials_are_redacted() -> None:
    message = _message("password=secret smb://u:p@nas/a")
    assert "secret" not in message
    assert "u:p" not in message


def test_structured_sensitive_values_and_auth_headers_are_redacted() -> None:
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "context=%s", ({
        "PASSWORD": "pässword",
        "authorization": "Bearer top-secret",
        "ticket": b"ticket-secret",
        "task_id": "task-1",
        "item_id": "item-1",
        "error_category": "AUTH",
        "error_code": "permission_denied",
    },), None)
    RedactingFilter().filter(record)
    message = record.getMessage()
    assert "pässword" not in message
    assert "top-secret" not in message
    assert "ticket-secret" not in message
    assert "task-1" in message and "item-1" in message
    assert "AUTH" in message and "permission_denied" in message


def test_bytes_args_paths_and_exception_are_safe() -> None:
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "%s %s", (b"password=secret", "/Users/alice/private/file.bin"), None
    )
    try:
        raise ValueError("authorization=Bearer hidden")
    except ValueError:
        record.exc_info = __import__("sys").exc_info()
    RedactingFilter().filter(record)
    message = record.getMessage()
    assert "secret" not in message
    assert "/Users/alice/private" not in message
    assert "file.bin" in message
    assert "hidden" not in str(record.exc_info)


def test_remote_path_and_sequence_values_keep_only_filename() -> None:
    record = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "%s %s",
        ({"remote_path": "share/private/report.txt"}, ["smb://u:p@nas/share/private/report.txt"]),
        None,
    )
    RedactingFilter().filter(record)
    message = record.getMessage()
    assert "share/private" not in message
    assert "u:p" not in message
    assert "report.txt" in message


def test_log_target_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "nasmove.log"
    target.symlink_to(tmp_path / "outside.log")
    try:
        configure_logging(tmp_path)
    except OSError:
        pass
    else:
        raise AssertionError("symlink log target must be rejected")


def test_configure_logging_is_private_and_idempotent(tmp_path: Path) -> None:
    logger = configure_logging(tmp_path)
    logger_again = configure_logging(tmp_path)
    log_file = tmp_path / "nasmove.log"
    assert logger is logger_again
    assert log_file.is_file()
    assert log_file.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert len([handler for handler in logger.handlers if getattr(handler, "_nasmove_secure_handler", False)]) == 1
