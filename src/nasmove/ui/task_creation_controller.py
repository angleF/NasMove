from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any, cast

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QInputDialog, QMessageBox, QProgressDialog

from nasmove.core.states import ConflictPolicy, TaskState, TransferAction, VerificationPolicy
from nasmove.planning.task_planner import (
    PlanRequest,
    PreflightCancellation,
    PreflightProgress,
)
from nasmove.ui.connection_page import ConnectionPage
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage
from nasmove.ui.task_presentation import ERROR_TEXT, safe_code
from nasmove.ui.worker import BackgroundCommandWorker


class TaskCreationController(QObject):
    """Build and persist one plan without blocking the Qt UI thread."""

    task_created = Signal(object)
    creating_changed = Signal(bool)
    preflight_progress = Signal(object)

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
        confirm_preflight: Callable[[object], bool] | None = None,
        conflict_policy_provider: Callable[[], ConflictPolicy] | None = None,
        resolve_ask_policy: Callable[[], ConflictPolicy | None] | None = None,
    ) -> None:
        super().__init__(target_page)
        self._connection_page = connection_page
        self._source_page = source_page
        self._target_page = target_page
        self._planner = planner
        self._repository = repository
        self._application = application
        self._confirm_move = confirm_move or self._confirm_move_dialog
        self._confirm_preflight = confirm_preflight or self._confirm_preflight_dialog
        self._conflict_policy_provider = (
            conflict_policy_provider or (lambda: ConflictPolicy.KEEP_BOTH)
        )
        self._resolve_ask_policy = resolve_ask_policy or self._ask_conflict_policy_dialog
        self._thread: QThread | None = None
        self._worker: BackgroundCommandWorker | None = None
        self._pending_session: object | None = None
        self._preflight_cancellation: PreflightCancellation | None = None
        self._progress_dialog: QProgressDialog | None = None
        self._creation_active = False
        self.preflight_progress.connect(self._update_preflight_progress)
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
        if request.conflict_policy is ConflictPolicy.ASK:
            resolved_policy = self._resolve_ask_policy()
            if resolved_policy is None:
                self._target_page.creation_status_label.setText(
                    "已取消选择冲突策略，任务未创建"
                )
                return
            if resolved_policy is ConflictPolicy.ASK:
                self._target_page.creation_status_label.setText("请选择一种确定的冲突策略")
                return
            request = replace(request, conflict_policy=resolved_policy)
        supports_preflight = callable(getattr(self._planner, "preflight", None)) and callable(
            getattr(self._planner, "confirm_preflight", None)
        )
        if not supports_preflight and request.action is TransferAction.MOVE and not self._confirm_move():
            self._target_page.creation_status_label.setText("已取消创建移动任务")
            return
        self._creation_active = True
        self._target_page.add_to_queue_button.setEnabled(False)
        self.creating_changed.emit(True)
        if supports_preflight:
            self._start_preflight(request)
            return
        self._target_page.creation_status_label.setText("正在规划任务…")

        def plan() -> object:
            try:
                return cast(Any, self._planner).plan(request)
            finally:
                release = getattr(self._repository, "release_thread_connection", None)
                if callable(release):
                    release()
        self._start_worker(plan, self._persist_and_enqueue, self._show_error)

    def _start_preflight(self, request: PlanRequest) -> None:
        cancellation = PreflightCancellation()
        self._preflight_cancellation = cancellation
        dialog = QProgressDialog(
            "正在扫描来源…",
            "取消预检",
            0,
            0,
            self._connection_page.window(),
        )
        dialog.setWindowTitle("正在预检迁移任务")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(cancellation.cancel)
        dialog.show()
        self._progress_dialog = dialog
        self._target_page.creation_status_label.setText("正在预检来源和 NAS 空间…")

        def preflight() -> object:
            try:
                return cast(Any, self._planner).preflight(
                    request,
                    cancellation=cancellation,
                    on_progress=self.preflight_progress.emit,
                )
            finally:
                release = getattr(self._repository, "release_thread_connection", None)
                if callable(release):
                    release()

        self._start_worker(preflight, self._preflight_ready, self._preflight_failed)

    def _start_worker(
        self,
        command: Callable[[], object],
        succeeded: Callable[[object], None],
        failed: Callable[[str], None],
    ) -> None:
        worker = BackgroundCommandWorker(command)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(succeeded)
        worker.failed.connect(failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._worker, self._thread = worker, thread
        thread.start()

    @Slot(object)
    def _preflight_ready(self, session: object) -> None:
        self._close_progress_dialog()
        self._pending_session = session
        self._target_page.creation_status_label.setText("预检完成，等待确认")

    @Slot(str)
    def _preflight_failed(self, error: str) -> None:
        self._close_progress_dialog()
        if error.startswith("PreflightCancelled:"):
            self._target_page.creation_status_label.setText("已取消预检，任务未创建")
            return
        self._show_error(error)

    def _review_preflight(self, session: object) -> None:
        if not self._confirm_preflight(session):
            cast(Any, self._planner).cancel_preflight(session)
            self._target_page.creation_status_label.setText("已取消预检，任务未创建")
            self._finish_creation()
            return
        self._target_page.creation_status_label.setText("正在创建任务…")

        def confirm() -> object:
            try:
                return cast(Any, self._planner).confirm_preflight(session)
            finally:
                release = getattr(self._repository, "release_thread_connection", None)
                if callable(release):
                    release()

        self._start_worker(confirm, self._persist_and_enqueue, self._show_error)

    def _request(self) -> PlanRequest:
        connection = self._connection_page.connection_config()
        sources = tuple(self._source_page.sources)
        target = self._target_page.target_path()
        move = self._source_page.move_checkbox.isChecked()
        name = ""
        if sources:
            first_name = sources[0].name or str(sources[0])
            if len(sources) == 1:
                name = first_name
            else:
                name = f"{first_name} 等 {len(sources)} 个项目"
        elif target:
            target_str = str(target).strip().rstrip("/\\")
            if target_str:
                name = PurePosixPath(target_str).name
        if not name:
            name = self._connection_page.display_name_lineedit.text().strip() or "NasMove 任务"
        return PlanRequest(
            name=name,
            connection=connection,
            sources=sources,
            target_root=target,
            action=TransferAction.MOVE if move else TransferAction.COPY,
            conflict_policy=self._conflict_policy_provider(),
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
            "移动会在完整校验并提交目标后将源文件移入废纸篓。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _ask_conflict_policy_dialog(self) -> ConflictPolicy | None:
        choices = {
            "保留两者（自动重命名）": ConflictPolicy.KEEP_BOTH,
            "覆盖目标": ConflictPolicy.OVERWRITE,
            "跳过同名项": ConflictPolicy.SKIP,
            "仅源文件较新时覆盖": ConflictPolicy.OVERWRITE_IF_NEWER,
        }
        selected, accepted = QInputDialog.getItem(
            self._connection_page.window(),
            "选择本次冲突策略",
            "检测到同名目标时如何处理？",
            tuple(choices),
            0,
            False,
        )
        return choices.get(selected) if accepted else None

    def _confirm_preflight_dialog(self, session: object) -> bool:
        summary = cast(Any, session).summary
        request = cast(Any, session).request
        action_text = (
            "移动：完整校验并提交 NAS 目标后将源文件移入废纸篓"
            if request.action is TransferAction.MOVE
            else "复制：源文件将保留在 Mac"
        )
        target_obj = getattr(request, "target_root", None)
        target_val = (
            getattr(target_obj, "value", str(target_obj))
            if target_obj is not None
            else ""
        )
        policy_obj = getattr(request, "conflict_policy", None)
        policy_val = (
            getattr(policy_obj, "value", str(policy_obj))
            if policy_obj is not None
            else ""
        )
        message = QMessageBox(self._connection_page.window())
        message.setIcon(QMessageBox.Icon.Question)
        message.setWindowTitle("确认迁移计划")
        message.setText("真实预检已完成，确认后才会创建任务。")
        info_lines = [f"来源：{len(request.sources)} 个"]
        if target_val:
            info_lines.append(f"目标：NAS · /{target_val}")
        info_lines.extend(
            [
                f"文件数：{summary.task.total_files}",
                f"扫描条目：{summary.item_count}",
                f"总大小：{self._format_bytes(summary.total_bytes)}",
                f"安全余量：{self._format_bytes(summary.safety_margin)}",
                f"所需空间：{self._format_bytes(summary.required_space)}",
                f"NAS 可用：{self._format_bytes(summary.free_space)}",
                f"冲突：{summary.conflict_count}",
            ]
        )
        if policy_val:
            policy_labels = {
                "keep_both": "保留两者（自动重命名）",
                "overwrite": "覆盖目标",
                "skip": "跳过同名项",
                "overwrite_if_newer": "仅源文件较新时覆盖",
                "ask": "每次创建时询问",
            }
            info_lines.append(f"冲突策略：{policy_labels.get(policy_val, policy_val)}")
        info_lines.append(action_text)
        message.setInformativeText("\n".join(info_lines))
        message.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        message.setDefaultButton(QMessageBox.StandardButton.Cancel)
        message.button(QMessageBox.StandardButton.Ok).setText("确认加入队列")
        message.button(QMessageBox.StandardButton.Cancel).setText("取消")
        return message.exec() == QMessageBox.StandardButton.Ok

    @staticmethod
    def _format_bytes(value: int) -> str:
        size = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if size < 1024 or unit == "TiB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{value} B"

    @Slot(object)
    def _update_preflight_progress(self, progress: object) -> None:
        if self._progress_dialog is None or not isinstance(progress, PreflightProgress):
            return
        self._progress_dialog.setLabelText(
            f"正在扫描：{progress.current_path.name}\n"
            f"已发现 {progress.item_count} 个条目，"
            f"{progress.total_files} 个文件，{self._format_bytes(progress.total_bytes)}"
        )

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
        if "insufficient NAS free space" in error:
            code = "disk_full"
        else:
            code = safe_code(error)
        self._target_page.creation_status_label.setText("任务未创建：" + ERROR_TEXT[code])

    @Slot()
    def _thread_finished(self) -> None:
        self._worker = None
        self._thread = None
        if self._pending_session is not None:
            session = self._pending_session
            self._pending_session = None
            self._review_preflight(session)
            return
        self._finish_creation()

    def _finish_creation(self) -> None:
        if not self._creation_active:
            return
        self._creation_active = False
        self._preflight_cancellation = None
        self._target_page.add_to_queue_button.setEnabled(True)
        self.creating_changed.emit(False)

    def _close_progress_dialog(self) -> None:
        if self._progress_dialog is None:
            return
        self._progress_dialog.close()
        self._progress_dialog.deleteLater()
        self._progress_dialog = None


__all__ = ["TaskCreationController"]
