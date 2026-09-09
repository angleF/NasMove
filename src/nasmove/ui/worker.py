from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from nasmove.smb.error_mapping import redacted_error_code


class BackgroundCommandWorker(QObject):
    """Run one callable on a QThread-owned object, never on the UI thread."""

    succeeded = Signal(object)
    failed = Signal(str)
    failed_code = Signal(str)
    cancelled = Signal()
    finished = Signal()

    def __init__(self, command: Callable[[], Any]) -> None:
        super().__init__()
        self._command = command
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        try:
            if self._cancelled:
                self.cancelled.emit()
                return
            result = self._command()
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)
        except Exception as error:  # noqa: BLE001 - UI boundary converts to text
            self.failed_code.emit(redacted_error_code(error))
            self.failed.emit(type(error).__name__ + ": " + str(error))
        finally:
            self.finished.emit()
