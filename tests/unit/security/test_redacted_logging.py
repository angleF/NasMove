from __future__ import annotations

import logging
import os
import sys
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


def test_auth_headers_are_redacted_before_generic_authorization_key() -> None:
    message = _message("Authorization: Bearer one Authorization:Basic two NTLM three")
    assert "one" not in message and "two" not in message and "three" not in message


def test_nested_auth_variants_fail_closed_but_allowed_context_survives() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s", ({
        "Auth": {
            "passphrase": "a",
            "token": "b",
            "secret_value": "c",
            "authorization_header": "Bearer d",
            "ntlm_response": "e",
            "ticket": {"session_key_id": "f"},
        },
        "task_id": "task-1",
        "item_id": "item-1",
        "error_category": "AUTH",
        "error_code": "permission_denied",
    },), None)
    RedactingFilter().filter(record)
    message = record.getMessage()
    for value in ("'a'", "'b'", "'c'", "'d'", "'e'", "'f'"):
        assert value not in message
    assert "task-1" in message and "AUTH" in message


def test_preset_exception_text_is_removed_and_exception_is_safe() -> None:
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), None)
    record.exc_text = "ValueError: password=leaked /Users/alice/private/file.txt"
    try:
        raise ValueError("raw-password")
    except ValueError:
        record.exc_info = sys.exc_info()
    RedactingFilter().filter(record)
    assert record.exc_text is None
    assert record.exc_info is None


def test_filter_is_fail_closed_for_cycles_and_bad_repr() -> None:
    class Bad:
        def __repr__(self) -> str:
            raise RuntimeError("secret")

        def __str__(self) -> str:
            raise RuntimeError("secret")

    cycle: list[object] = []
    cycle.append(cycle)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s %s", (cycle, Bad()), None)
    assert RedactingFilter().filter(record) is True
    assert "secret" not in record.getMessage()


def test_utf16_bytes_are_not_rendered_as_original_content() -> None:
    original = "密码-token".encode("utf-16")
    message = _message("payload=%s", (original,))
    assert original.hex() not in message
    assert "密码" not in message


def test_paths_with_spaces_are_replaced_as_whole_values() -> None:
    message = _message(
        "source_path=\"/Users/alice/My Private/file name.txt\" remote_path=\"share/My Private/file name.txt\""
    )
    assert "/Users/alice/My Private" not in message
    assert "share/My Private" not in message
    assert "file name.txt" in message


def test_existing_handlers_and_children_cannot_bypass_redaction(tmp_path: Path) -> None:
    logger = logging.getLogger("nasmove")
    logger.handlers.clear()
    logger.propagate = True
    ordinary = logging.FileHandler(tmp_path / "ordinary.log", encoding="utf-8")
    logger.addHandler(ordinary)
    configure_logging(tmp_path / "secure")
    logging.getLogger("nasmove.child").error("password=leaked")
    for handler in logger.handlers:
        handler.flush()
    ordinary.close()
    logger.removeHandler(ordinary)
    assert "leaked" not in (tmp_path / "ordinary.log").read_text()


def test_recursive_log_directories_are_private_and_wide_existing_parent_unchanged(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    os.chmod(existing, 0o755)
    nested = existing / "a" / "b"
    try:
        configure_logging(nested)
    except OSError:
        pass
    else:
        raise AssertionError("wide existing parent must be rejected")
    assert existing.stat().st_mode & 0o777 == 0o755


def test_idempotent_configure_rechecks_and_repairs_file_mode(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    os.chmod(tmp_path / "nasmove.log", 0o644)
    configure_logging(tmp_path)
    assert (tmp_path / "nasmove.log").stat().st_mode & 0o777 == 0o600
