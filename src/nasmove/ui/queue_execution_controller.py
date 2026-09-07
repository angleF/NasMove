from __future__ import annotations

from typing import Any, cast

from PySide6.QtCore import QObject, QThread, Signal, Slot

from nasmove.ui.task_controller import TaskController


class _QueueWorker(QObject):
    result_ready = Signal(object)
    failed = Signal()
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
        except Exception:  # noqa: BLE001 - details belong in the redacted logger
            self.failed.emit()
        finally:
            self.finished.emit()


class QueueExecutionController(QObject):
    """Drain the serial queue on one background Qt thread."""

    def __init__(self, queue: object, task_controller: TaskController) -> None:
        super().__init__(task_controller)
        self._queue = queue
        self._tasks = task_controller
        self._thread: QThread | None = None
        self._worker: _QueueWorker | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    @Slot()
    def start(self) -> None:
        if self._thread is not None:
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


__all__ = ["QueueExecutionController"]
