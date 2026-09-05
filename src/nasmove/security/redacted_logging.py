from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from getpass import getuser
from io import TextIOWrapper
from pathlib import Path
from typing import Any, cast

_REDACTED = "<redacted>"
_REDACTED_BYTES = "<redacted-bytes>"
_MAX_DEPTH = 16
_MAX_ITEMS = 256
_MACOS = sys.platform == "darwin"
_DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "NasMove"
_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
_SENSITIVE_KEY_BASES = {
    "password",
    "passwd",
    "passphrase",
    "token",
    "secret",
    "authorization",
    "bearer",
    "basic",
    "ntlm",
    "ticket",
    "session_key",
    "hash",
    "username",
    "user_name",
}
_NORMALIZED_SENSITIVE_KEY_BASES = {key.replace("-", "_") for key in _SENSITIVE_KEY_BASES}
_ALLOWED_EXTRA_KEYS = {"task_id", "item_id", "error_category", "error_code"}
_PATH_KEYS = {"path", "source", "target", "source_path", "target_path", "remote_path", "local_path"}
_STANDARD_RECORD_FIELDS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info", "thread", "threadName",
}

_AUTH_HEADER_RE = re.compile(r"(?i)\b(?:authorization\s*[:=]\s*)?(?:bearer|basic|ntlm)\s+[^\s,;]+")
_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<key>password|passwd|passphrase|token|secret(?:[_-][^\s:=]+)?|authorization(?:[_-]header)?|"
    r"bearer|basic|ntlm(?:[_-]response)?|ticket|session[_-]?key(?:[_-]id)?|hash|username|user_name)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_SMB_URL_RE = re.compile(
    r"(?i)(?P<scheme>(?:smb|cifs)://)(?P<userinfo>[^/\\\s@]+@)?(?P<host>[^/\\\s]+)(?P<path>/[^\r\n]*)?"
)
_UNC_RE = re.compile(
    r"(?i)(?P<prefix>\\\\)(?P<userinfo>[^\\/\s@]+@)?(?P<host>[^\\/\s]+)(?P<path>\\[^\r\n]*)?"
)
_PATH_VALUE_RE = re.compile(
    r"(?i)(?P<key>source[_-]?path|target[_-]?path|remote[_-]?path|local[_-]?path|path)"
    r"\s*[:=]\s*(?P<value>\"[^\"]*\"|'[^']*'|[^\r\n]*)"
)
_LOCAL_PATH_RE = re.compile(r"(?<![\w:])/(?:[^/\s]+/)+(?P<name>[^/\s]+)")
_LOCAL_PATH_WITH_SPACES_RE = re.compile(r"(?<![\w:])(?P<path>/(?:Users|Volumes|private|tmp)/[^\r\n]*)")


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(
        normalized == base
        or normalized.startswith(base + "_")
        or normalized.endswith("_" + base)
        for base in _NORMALIZED_SENSITIVE_KEY_BASES
    )


def _path_basename(value: str) -> str:
    clean = value.strip("\"'").rstrip("/\\")
    return re.split(r"[/\\]", clean)[-1] or "<path>"


def _redact_text(value: str) -> str:
    protected: list[str] = []

    def protect(replacement: str) -> str:
        protected.append(replacement)
        return f"__NASMOVE_PROTECTED_{len(protected) - 1}__"

    def replace_url(match: re.Match[str]) -> str:
        userinfo = _REDACTED + "@" if match.group("userinfo") else ""
        return protect(f"{match.group('scheme')}{userinfo}{match.group('host')}/<path>")

    value = _SMB_URL_RE.sub(replace_url, value)

    def replace_unc(match: re.Match[str]) -> str:
        userinfo = _REDACTED + "@" if match.group("userinfo") else ""
        return protect(f"{match.group('prefix')}{userinfo}{match.group('host')}\\<path>")

    value = _UNC_RE.sub(replace_unc, value)
    value = _AUTH_HEADER_RE.sub(lambda match: _REDACTED, value)

    def replace_path_key(match: re.Match[str]) -> str:
        return f"{match.group('key')}=<path>/{_path_basename(match.group('value'))}"

    value = _PATH_VALUE_RE.sub(replace_path_key, value)
    value = _LOCAL_PATH_WITH_SPACES_RE.sub(lambda match: f"<path>/{_path_basename(match.group('path'))}", value)
    value = _LOCAL_PATH_RE.sub(lambda match: f"<path>/{match.group('name')}", value)
    value = _KEY_VALUE_RE.sub(lambda match: f"{match.group('key')}={_REDACTED}", value)
    value = _LOCAL_PATH_RE.sub(lambda match: f"<path>/{match.group('name')}", value)
    for index, replacement in enumerate(protected):
        value = value.replace(f"__NASMOVE_PROTECTED_{index}__", replacement)
    return value


def _redact_path_value(value: str) -> str:
    return f"<path>/{_path_basename(value)}"


def _redact_value(value: Any, *, key: str | None = None, visited: set[int] | None = None, depth: int = 0) -> Any:
    if visited is None:
        visited = set()
    try:
        normalized_key = key.lower().replace("-", "_") if key is not None else None
        if normalized_key in _ALLOWED_EXTRA_KEYS and isinstance(value, (str, int)):
            return value
        if normalized_key is not None and _is_sensitive_key(normalized_key):
            return _REDACTED
        if normalized_key in _PATH_KEYS and isinstance(value, str):
            return _redact_path_value(value)
        if depth > _MAX_DEPTH:
            return _REDACTED
        if isinstance(value, str):
            return _redact_text(value)
        if isinstance(value, bytes):
            return _REDACTED_BYTES
        if isinstance(value, BaseException):
            return f"{type(value).__name__} ({_REDACTED})"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        object_id = id(value)
        if object_id in visited:
            return _REDACTED
        visited.add(object_id)
        try:
            if isinstance(value, Mapping):
                items = list(value.items())[:_MAX_ITEMS]
                return {
                    _redact_value(item_key, visited=visited, depth=depth + 1): _redact_value(
                        item, key=str(item_key), visited=visited, depth=depth + 1
                    )
                    for item_key, item in items
                }
            if isinstance(value, tuple):
                return tuple(_redact_value(item, visited=visited, depth=depth + 1) for item in value[:_MAX_ITEMS])
            if isinstance(value, list):
                return [_redact_value(item, visited=visited, depth=depth + 1) for item in value[:_MAX_ITEMS]]
            if isinstance(value, (set, frozenset)):
                redacted = [_redact_value(item, visited=visited, depth=depth + 1) for item in list(value)[:_MAX_ITEMS]]
                return type(value)(redacted)
            if isinstance(value, Sequence):
                return tuple(_redact_value(item, visited=visited, depth=depth + 1) for item in value[:_MAX_ITEMS])
            try:
                rendered = str(value)
            except BaseException:  # noqa: BLE001 - hostile object formatting must fail closed
                return _REDACTED
            return _redact_text(rendered)
        finally:
            visited.discard(object_id)
    except BaseException:  # noqa: BLE001 - recursive sanitization must never escape
        return _REDACTED


class RedactingFilter(logging.Filter):
    """Remove credentials and private paths before a record reaches any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _redact_value(record.msg)
            record.args = _redact_value(record.args)
            record.exc_info = None
            record.exc_text = None
            if record.stack_info:
                record.stack_info = _redact_text(record.stack_info)
            for field_name, field_value in list(record.__dict__.items()):
                if field_name in _STANDARD_RECORD_FIELDS:
                    continue
                if field_name in _ALLOWED_EXTRA_KEYS:
                    setattr(record, field_name, _redact_value(field_value, key=field_name))
                else:
                    setattr(record, field_name, _REDACTED)
        except BaseException:  # noqa: BLE001 - filter failures must never escape
            record.msg = _REDACTED
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def _run_acl_command(arguments: list[str]) -> str:
    result = subprocess.run(
        arguments, check=True, capture_output=True, text=True, shell=False, env={"LC_ALL": "C"}
    )
    return result.stdout


def _verify_acl(path: Path) -> None:
    if not _MACOS:
        return
    try:
        output = _run_acl_command(["/bin/ls", "-lde", str(path)])
    except FileNotFoundError:
        return
    lines = output.splitlines()[1:]
    current_user = getuser()
    for line in lines:
        entry = line.strip().lower()
        if "everyone" in entry:
            raise OSError("log target has an unsafe ACL")
        if ": user:" in entry and f"user:{current_user.lower()}" not in entry:
            raise OSError("log target has an unsafe ACL")
        if ": group:" in entry:
            raise OSError("log target has an unsafe ACL")


def _clear_acl(path: Path) -> None:
    if _MACOS:
        _run_acl_command(["/bin/chmod", "-N", str(path)])
        _verify_acl(path)


def _check_owner_regular(path: Path, expected_mode: int | None = None) -> os.stat_result:
    stat_result = path.lstat()
    if stat.S_ISLNK(stat_result.st_mode) or not stat.S_ISREG(stat_result.st_mode):
        raise OSError("log target must be a regular non-symlink")
    if hasattr(os, "getuid") and stat_result.st_uid != os.getuid():
        raise OSError("log target is not owned by the current user")
    if expected_mode is not None and stat_result.st_mode & 0o777 != expected_mode:
        os.chmod(path, expected_mode)
        stat_result = path.lstat()
        if stat_result.st_mode & 0o777 != expected_mode:
            raise OSError("log target mode is not private")
    _verify_acl(path)
    return stat_result


def _private_ancestor(path: Path) -> bool:
    resolved = path.resolve()
    value = str(resolved)
    temp_root = str(_TEMP_ROOT)
    return (
        resolved in {Path.home(), Path.home() / "Library", Path.home() / "Library" / "Logs"}
        or value in {"/", "/private", "/tmp", "/private/tmp", "/var", "/private/var"}
        or value == "/private/var/folders"
        or value == temp_root
        or temp_root.startswith(value + "/")
    )


def _ensure_private_directory(path: Path) -> None:
    path = path.absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise OSError("log directory must be a real directory")
        if current.exists():
            stat_result = current.lstat()
            if hasattr(os, "getuid") and stat_result.st_uid != os.getuid() and not _private_ancestor(current):
                raise OSError("existing log directory is not owned by the current user")
            mode = stat_result.st_mode & 0o777
            if mode & 0o077 and not _private_ancestor(current):
                raise OSError("existing log directory is not private")
            _verify_acl(current)
            continue
        current.mkdir(mode=0o700)
        os.chmod(current, 0o700)
        stat_result = current.lstat()
        if stat_result.st_mode & 0o777 != 0o700 or (
            hasattr(os, "getuid") and stat_result.st_uid != os.getuid()
        ):
            raise OSError("new log directory is not private")
        _clear_acl(current)


class _SecureFileHandler(logging.FileHandler):
    _nasmove_secure_handler = True

    def _open(self) -> TextIOWrapper:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        target = Path(self.baseFilename)
        existed = target.exists()
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            stat_result = os.fstat(fd)
            if not stat.S_ISREG(stat_result.st_mode):
                raise OSError("log target is not a regular file")
            if hasattr(os, "getuid") and stat_result.st_uid != os.getuid():
                raise OSError("log target is not owned by the current user")
            os.fchmod(fd, 0o600)
            if existed:
                _verify_acl(target)
            else:
                _clear_acl(target)
            _check_owner_regular(target, expected_mode=0o600)
            return cast(TextIOWrapper, os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors))
        except BaseException:
            os.close(fd)
            raise


def _attach_filter(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        if not any(getattr(item, "_nasmove_redactor", False) for item in handler.filters):
            redactor = RedactingFilter()
            redactor._nasmove_redactor = True  # type: ignore[attr-defined]
            handler.addFilter(redactor)


def configure_logging(log_dir: Path | None = None) -> logging.Logger:
    """Configure one private NasMove diagnostics handler and secure logger descendants."""
    target_dir = Path(log_dir) if log_dir is not None else Path.home() / "Library" / "Logs" / "NasMove"
    _ensure_private_directory(target_dir)
    target = target_dir / "nasmove.log"
    if target.exists() or target.is_symlink():
        _check_owner_regular(target, expected_mode=0o600)

    logger = logging.getLogger("nasmove")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger_dict = logging.Logger.manager.loggerDict
    for name, candidate in list(logger_dict.items()):
        if (name == "nasmove" or name.startswith("nasmove.")) and isinstance(candidate, logging.Logger):
            candidate.propagate = True
            _attach_filter(candidate)
    target_name = str(target.absolute())
    existing = [handler for handler in logger.handlers if getattr(handler, "_nasmove_secure_handler", False)]
    for handler in existing:
        if getattr(handler, "baseFilename", None) == target_name:
            _check_owner_regular(target, expected_mode=0o600)
            _attach_filter(logger)
            return logger
        logger.removeHandler(handler)
        handler.close()
    handler = _SecureFileHandler(target_name, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _attach_filter(logger)
    logger.addHandler(handler)
    return logger
