from __future__ import annotations

from typing import Any, cast

from PySide6.QtCore import QObject, QThread, Signal, Slot

from nasmove.smb.error_mapping import redacted_error_code
from nasmove.ui.task_controller import TaskController


class _QueueWorker(QObject):
    result_ready = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(self, queue: object) -> None:
        super().__init__()
        self._queue = queue

    @Slot()
    def run(self) -> None:
        try:
            while True:
                result = cast(Any, self._queue).run_next()
                if result is None:
                    return
                self.result_ready.emit(result)
        except Exception as error:  # noqa: BLE001 - send only an allowlisted code
            self.failed.emit((getattr(self._queue, "last_task_id", None), redacted_error_code(error)))
        finally:
            release = getattr(self._queue, "release_thread_connection", None)
            if callable(release):
                release()
            self.finished.emit()


class QueueExecutionController(QObject):
    """Drain the serial queue on one background Qt thread."""

    def __init__(self, queue: object, task_controller: TaskController) -> None:
        super().__init__(task_controller)
        self._queue = queue
        self._tasks = task_controller
        self._thread: QThread | None = None
        self._worker: _QueueWorker | None = None
        self._restart_requested = False
        task_controller.resume_execution.connect(self.start)

    @property
    def running(self) -> bool:
        return self._thread is not None

    @Slot()
    def start(self) -> None:
        if self._thread is not None:
            self._restart_requested = True
            return
        worker = _QueueWorker(self._queue)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result_ready.connect(self._tasks.publish_result)
        worker.failed.connect(self._tasks.publish_execution_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._worker, self._thread = worker, thread
        thread.start()

    @Slot()
    def _thread_finished(self) -> None:
        self._worker = None
        self._thread = None
        restart, self._restart_requested = self._restart_requested, False
        pending = getattr(self._queue, "has_pending_tasks", None)
        if restart and callable(pending):
            try:
                if pending():
                    self.start()
            except Exception as error:  # noqa: BLE001 - startup failure is user-visible
                self._tasks.publish_execution_error((None, redacted_error_code(error)))


__all__ = ["QueueExecutionController"]
