from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO
from uuid import uuid4

from nasmove.core.model import RemotePath
from nasmove.core.ports import SmbGateway
from nasmove.smb.error_mapping import redacted_error_code

_FIRST_SEGMENT = b"NASMOVE-CAPABILITY-PROBE-FIRST\x00\x01\x02"
_SECOND_SEGMENT = b"NASMOVE-CAPABILITY-PROBE-SECOND\xfd\xfe\xff"
_INITIAL_CONTENT = _FIRST_SEGMENT + _SECOND_SEGMENT
_APPENDED_CONTENT = b"NASMOVE-RANDOM-OFFSET-APPEND"
_TRUNCATED_SIZE = len(_INITIAL_CONTENT) + len(_APPENDED_CONTENT) - 7
_EXPECTED_CONTENT = (_INITIAL_CONTENT + _APPENDED_CONTENT)[:_TRUNCATED_SIZE]


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    random_write: bool
    flush: bool
    truncate: bool
    rename_exclusive: bool
    cleanup: bool
    error_code: str | None = None

    @property
    def all_supported(self) -> bool:
        return all(
            (
                self.random_write,
                self.flush,
                self.truncate,
                self.rename_exclusive,
                self.cleanup,
            )
        )

    @classmethod
    def failed(cls, error_code: str) -> CapabilityReport:
        return cls(False, False, False, False, False, error_code)


class SmbCapabilityProbe:
    def __init__(self, gateway: SmbGateway) -> None:
        self._gateway = gateway

    def run(self, target: RemotePath) -> CapabilityReport:
        probe_id = uuid4().hex
        original = RemotePath(f"{target.value}/.nasmove-probe-{probe_id}.tmp")
        renamed = RemotePath(f"{target.value}/.nasmove-probe-{probe_id}.renamed")
        created = False
        rename_attempted = False
        was_renamed = False
        flush_supported = False
        rename_supported = False
        random_write_supported = False
        truncate_supported = False
        error_code: str | None = None
        phase = "create"

        try:
            with self._gateway.create_exclusive(original) as stream:
                created = True
                _write_fully(stream, _INITIAL_CONTENT)
                phase = "flush"
                stream.flush()
                flush_supported = True

            phase = "random_write"
            with self._gateway.open_update(original) as stream:
                if stream.read() != _INITIAL_CONTENT:
                    raise _ContentMismatchError
                stream.seek(len(_INITIAL_CONTENT))
                _write_fully(stream, _APPENDED_CONTENT)

            phase = "truncate"
            self._gateway.truncate(original, _TRUNCATED_SIZE)
            phase = "rename_exclusive"
            rename_attempted = True
            self._gateway.rename_exclusive(original, renamed)
            was_renamed = True
            rename_supported = True

            phase = "verify"
            with self._gateway.open_read(renamed) as stream:
                if stream.read() != _EXPECTED_CONTENT:
                    raise _ContentMismatchError
            random_write_supported = True
            truncate_supported = True
        except Exception as error:  # noqa: BLE001 - the probe converts boundary errors to safe codes
            error_code = f"{phase}:{_probe_error_code(error)}"

        cleanup_supported, cleanup_error = self._cleanup(
            original=original,
            renamed=renamed,
            created=created,
            rename_attempted=rename_attempted,
            was_renamed=was_renamed,
        )
        if cleanup_error is not None:
            error_code = f"cleanup:{cleanup_error}"

        return CapabilityReport(
            random_write=random_write_supported,
            flush=flush_supported,
            truncate=truncate_supported,
            rename_exclusive=rename_supported,
            cleanup=cleanup_supported,
            error_code=error_code,
        )

    def _cleanup(
        self,
        *,
        original: RemotePath,
        renamed: RemotePath,
        created: bool,
        rename_attempted: bool,
        was_renamed: bool,
    ) -> tuple[bool, str | None]:
        if not created:
            return False, None
        candidates = [renamed] if was_renamed else [original]
        cleanup_error: str | None = None
        for candidate in candidates:
            try:
                self._gateway.remove_file(candidate)
            except FileNotFoundError:
                continue
            except Exception as error:  # noqa: BLE001 - cleanup failures must be reported
                cleanup_error = redacted_error_code(error)
        rename_result_is_certain = not rename_attempted or was_renamed
        return cleanup_error is None and rename_result_is_certain, cleanup_error


class _ContentMismatchError(Exception):
    pass


def _probe_error_code(error: BaseException) -> str:
    if isinstance(error, _ContentMismatchError):
        return "content_mismatch"
    return redacted_error_code(error)


def _write_fully(stream: BinaryIO, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = stream.write(data[offset:])
        if not isinstance(written, int) or written <= 0:
            raise OSError("SMB write made no progress")
        offset += written
