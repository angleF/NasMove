from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_REDACTED = "<redacted>"
_DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "NasMove"
_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "authorization",
    "ntlm",
    "ticket",
    "session_key",
    "session-key",
    "username",
    "user_name",
}
_PATH_KEYS = {"path", "source", "target", "source_path", "target_path", "remote_path", "local_path"}
_NORMALIZED_SENSITIVE_KEYS = {candidate.replace("-", "_") for candidate in _SENSITIVE_KEYS}
_ALLOWED_EXTRA_KEYS = {"task_id", "item_id", "error_category", "error_code"}
_STANDARD_RECORD_FIELDS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}

_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<key>password|passwd|authorization|ntlm|ticket|session[_-]?key|username|user_name)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_AUTH_RE = re.compile(r"(?i)\b(?P<scheme>bearer|basic|ntlm)\s+[^\s,;]+")
_SMB_URL_RE = re.compile(
    r"(?i)(?P<scheme>(?:smb|cifs)://)(?P<userinfo>[^/\\\s@]+@)?"
    r"(?P<host>[^/\\\s]+)(?P<path>/[^\s]*)?"
)
_UNC_RE = re.compile(r"(?i)(?P<prefix>\\\\)(?P<userinfo>[^\\/\s@]+@)?(?P<host>[^\\/\s]+)(?P<path>\\[^\s]*)?")
_LOCAL_PATH_RE = re.compile(r"(?<![\w:])/(?:[^/\s]+/)+(?P<name>[^/\s]+)")
_PATH_VALUE_RE = re.compile(
    r"(?i)(?P<key>source[_-]?path|target[_-]?path|remote[_-]?path|local[_-]?path|path)"
    r"\s*[:=]\s*(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def _redact_text(value: str) -> str:
    value = _KEY_VALUE_RE.sub(lambda match: f"{match.group('key')}={_REDACTED}", value)
    value = _AUTH_RE.sub(lambda match: f"{match.group('scheme')} {_REDACTED}", value)
    protected: list[str] = []

    def replace_url(match: re.Match[str]) -> str:
        path = "/<path>" if match.group("path") else ""
        userinfo = _REDACTED + "@" if match.group("userinfo") else ""
        protected.append(f"{match.group('scheme')}{userinfo}{match.group('host')}{path}")
        return f"__NASMOVE_URL_{len(protected) - 1}__"

    value = _SMB_URL_RE.sub(replace_url, value)

    def replace_unc(match: re.Match[str]) -> str:
        path = "\\<path>" if match.group("path") else ""
        userinfo = _REDACTED + "@" if match.group("userinfo") else ""
        protected.append(f"{match.group('prefix')}{userinfo}{match.group('host')}{path}")
        return f"__NASMOVE_URL_{len(protected) - 1}__"

    value = _UNC_RE.sub(replace_unc, value)
    value = _LOCAL_PATH_RE.sub(lambda match: f"<path>/{match.group('name')}", value)

    def replace_path_key(match: re.Match[str]) -> str:
        raw_value = match.group("value")
        quote = raw_value[0] if raw_value[:1] in {"\"", "'"} else ""
        unquoted = raw_value[1:-1] if quote else raw_value
        name = re.split(r"[/\\]", unquoted)[-1] or "<path>"
        return f"{match.group('key')}={quote}<path>/{name}{quote}"

    value = _PATH_VALUE_RE.sub(replace_path_key, value)
    for index, replacement in enumerate(protected):
        value = value.replace(f"__NASMOVE_URL_{index}__", replacement)
    return value


def _redact_path_value(value: str) -> str:
    name = re.split(r"[/\\]", value.strip("\"'"))[-1]
    return f"<path>/{name or '<path>'}"


def _redact_value(value: Any, *, key: str | None = None) -> Any:
    normalized_key = key.lower().replace("-", "_") if key is not None else None
    if normalized_key in _NORMALIZED_SENSITIVE_KEYS:
        return _REDACTED
    if normalized_key in _PATH_KEYS and isinstance(value, str):
        return _redact_path_value(value)
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, bytes):
        return _redact_text(value.decode("utf-8", errors="replace")).encode("utf-8")
    if isinstance(value, Mapping):
        return {
            key: _redact_value(item, key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, set):
        return {_redact_value(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_redact_value(item) for item in value)
    if isinstance(value, Sequence):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, BaseException):
        return f"{type(value).__name__} ({_REDACTED})"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


class RedactingFilter(logging.Filter):
    """Remove credentials and private paths before a record reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_value(record.msg)
        record.args = _redact_value(record.args)
        if record.exc_info is not None:
            exception = record.exc_info[1]
            if exception is not None:
                summary = RuntimeError(f"{type(exception).__name__} ({_REDACTED})")
                record.exc_info = (type(exception), summary, None)
        if record.exc_text:
            record.exc_text = _redact_text(record.exc_text)
        for field_name, field_value in list(record.__dict__.items()):
            if field_name in _STANDARD_RECORD_FIELDS:
                continue
            if field_name.lower().replace("-", "_") in _NORMALIZED_SENSITIVE_KEYS:
                setattr(record, field_name, _REDACTED)
            elif field_name in _ALLOWED_EXTRA_KEYS:
                setattr(record, field_name, _redact_value(field_value))
            else:
                setattr(record, field_name, _REDACTED)
        if record.stack_info:
            record.stack_info = _redact_text(record.stack_info)
        return True


class _SecureFileHandler(logging.FileHandler):
    _nasmove_secure_handler = True

    def _open(self):  # type: ignore[no-untyped-def]
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            stat_result = os.fstat(fd)
            if not stat_result.st_mode & 0o100000:
                raise OSError("log target is not a regular file")
            if hasattr(os, "getuid") and stat_result.st_uid != os.getuid():
                raise OSError("log target is not owned by the current user")
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)
        except Exception:
            os.close(fd)
            raise


def _ensure_private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise OSError("log directory must be a real directory")
    else:
        path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    stat_result = path.stat()
    if hasattr(os, "getuid") and stat_result.st_uid != os.getuid():
        raise OSError("log directory is not owned by the current user")
    if stat_result.st_mode & 0o077:
        raise OSError("log directory is not private")


def configure_logging(log_dir: Path | None = None) -> logging.Logger:
    """Configure the single private NasMove diagnostics log handler."""
    target_dir = Path(log_dir) if log_dir is not None else _DEFAULT_LOG_DIR
    _ensure_private_directory(target_dir)
    target = target_dir / "nasmove.log"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise OSError("log target must be a regular file")

    logger = logging.getLogger("nasmove")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    target_name = str(target.resolve())
    existing = [handler for handler in logger.handlers if getattr(handler, "_nasmove_secure_handler", False)]
    for handler in existing:
        if getattr(handler, "baseFilename", None) == target_name:
            return logger
        logger.removeHandler(handler)
        handler.close()

    handler = _SecureFileHandler(target_name, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    return logger
