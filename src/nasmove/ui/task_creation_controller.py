from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QMessageBox

from nasmove.core.states import ConflictPolicy, TaskState, TransferAction, VerificationPolicy
from nasmove.planning.task_planner import PlanRequest
from nasmove.ui.connection_page import ConnectionPage
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage
from nasmove.ui.task_presentation import ERROR_TEXT, safe_code
from nasmove.ui.worker import BackgroundCommandWorker


class TaskCreationController(QObject):
    """Build and persist one plan without blocking the Qt UI thread."""

    task_created = Signal(object)

    def __init__(
        self,
        connection_page: ConnectionPage,
        source_page: SourcePage,
        target_page: TargetPage,
        *,
        planner: object,
        repository: object,
        application: object,
        confirm_move: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(target_page)
        self._connection_page = connection_page
        self._source_page = source_page
        self._target_page = target_page
        self._planner = planner
        self._repository = repository
        self._application = application
        self._confirm_move = confirm_move or self._confirm_move_dialog
        self._thread: QThread | None = None
        self._worker: BackgroundCommandWorker | None = None
        target_page.create_task_requested.connect(self.create_task)

    @Slot()
    def create_task(self) -> None:
        if self._thread is not None:
            return
        try:
            request = self._request()
        except Exception as error:  # noqa: BLE001 - UI validation boundary
            self._target_page.creation_status_label.setText(
                f"任务创建失败：{type(error).__name__}"
            )
            return
        if request.action is TransferAction.MOVE and not self._confirm_move():
            self._target_page.creation_status_label.setText("已取消创建移动任务")
            return
        self._target_page.add_to_queue_button.setEnabled(False)
        self._target_page.creation_status_label.setText("正在规划任务…")
        def plan() -> object:
            try:
                return cast(Any, self._planner).plan(request)
            finally:
                release = getattr(self._repository, "release_thread_connection", None)
                if callable(release):
                    release()
        worker = BackgroundCommandWorker(plan)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._persist_and_enqueue)
        worker.failed_code.connect(self._show_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._worker, self._thread = worker, thread
        thread.start()

    def _request(self) -> PlanRequest:
        connection = self._connection_page.connection_config()
        sources = tuple(self._source_page.sources)
        target = self._target_page.target_path()
        move = self._source_page.move_checkbox.isChecked()
        return PlanRequest(
            name=self._connection_page.display_name_lineedit.text().strip() or "NasMove 任务",
            connection=connection,
            sources=sources,
            target_root=target,
            action=TransferAction.MOVE if move else TransferAction.COPY,
            conflict_policy=ConflictPolicy.AUTO_RENAME,
            verification_policy=VerificationPolicy.FULL,
            queue_position=self._next_queue_position(),
        )

    def _next_queue_position(self) -> int:
        listing = getattr(self._repository, "list_incomplete_tasks", None)
        if not callable(listing):
            return 0
        positions = [
            int(cast(Any, task).queue_position)
            for task in cast(Any, listing)()
            if hasattr(task, "queue_position")
        ]
        return max(positions, default=-1) + 1

    def _confirm_move_dialog(self) -> bool:
        answer = QMessageBox.question(
            self._target_page,
            "确认移动任务",
            "移动会在完整校验并提交目标后删除源文件。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer is QMessageBox.StandardButton.Yes

    @Slot(object)
    def _persist_and_enqueue(self, planned: object) -> None:
        try:
            task = cast(Any, planned).task
            cast(Any, self._repository).transition_task(
                task.id,
                TaskState.PREFLIGHT,
                TaskState.QUEUED,
            )
            cast(Any, self._application).enqueue(task.id)
        except Exception as error:  # noqa: BLE001 - UI boundary
            self._show_error(f"{type(error).__name__}: {error}")
            return
        self._target_page.creation_status_label.setText("任务已加入队列")
        getter = getattr(self._repository, "get_task", None)
        queued_task = cast(Any, getter)(task.id) if callable(getter) else task
        self.task_created.emit(queued_task)

    @Slot(str)
    def _show_error(self, error: str) -> None:
        self._target_page.creation_status_label.setText("任务未创建：" + ERROR_TEXT[safe_code(error)])

    @Slot()
    def _thread_finished(self) -> None:
        self._worker = None
        self._thread = None
        self._target_page.add_to_queue_button.setEnabled(True)


__all__ = ["TaskCreationController"]
