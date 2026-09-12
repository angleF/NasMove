from __future__ import annotations

import errno
from dataclasses import replace
from pathlib import Path
from threading import Thread, get_ident

from PySide6.QtCore import QCoreApplication, Qt

from nasmove.core.errors import SourceFileMissingError
from nasmove.core.model import TaskId
from nasmove.core.states import TaskState
from nasmove.transfer.checkpoint_writer import CancellationToken
from nasmove.transfer.transfer_engine import TaskResult, TransferEvent
from nasmove.ui.queue_panel import QueuePanel
from nasmove.ui.task_controller import TaskController
from nasmove.ui.task_page import TaskPage
from tests.fixtures.builders import build_task_record, snapshot


class Commands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []

    def pause(self, task_id: object) -> None:
        self.calls.append(("pause", task_id))

    def resume(self, task_id: object) -> None:
        self.calls.append(("resume", task_id))

    def cancel(self, task_id: object) -> None:
        self.calls.append(("cancel", task_id))


def test_task_buttons_dispatch_commands_for_selected_task(qtbot) -> None:
    page = TaskPage()
    commands = Commands()
    controller = TaskController(page, commands=commands)
    qtbot.addWidget(page)
    task = build_task_record()

    controller.load_queue((task,))
    page.pause_requested.emit()
    page.resume_requested.emit()
    page.cancel_requested.emit()

    assert commands.calls == [("pause", task.id), ("resume", task.id), ("cancel", task.id)]


def test_attached_queue_panel_routes_commands_and_receives_events(qtbot) -> None:
    page = TaskPage()
    panel = QueuePanel()
    commands = Commands()
    controller = TaskController(page, commands=commands)
    qtbot.addWidget(page)
    qtbot.addWidget(panel)
    task = replace(build_task_record(), state=TaskState.RUNNING)

    controller.attach_queue_panel(panel)
    controller.load_queue((task,))
    panel.select_task(task.id)
    panel.pause_button.click()
    controller.publish(TransferEvent(task.id, TaskState.WAITING_FOR_NETWORK))

    qtbot.waitUntil(lambda: panel.state_text(task.id) == "等待网络")
    assert commands.calls == [("pause", task.id)]


def test_worker_thread_events_are_applied_on_qt_thread(qtbot) -> None:
    page = TaskPage()
    controller = TaskController(page)
    qtbot.addWidget(page)
    ui_thread = get_ident()
    applied_threads: list[int] = []
    controller.event_applied.connect(lambda: applied_threads.append(get_ident()))

    worker = Thread(
        target=lambda: controller.publish(
            TransferEvent(TaskId("task-1"), TaskState.WAITING_FOR_NETWORK, kind="retry")
        )
    )
    worker.start()
    worker.join()

    qtbot.waitUntil(lambda: page.status_label.text() == "等待网络")
    QCoreApplication.processEvents()
    assert applied_threads == [ui_thread]


def test_visible_task_event_publishes_workspace_state(qtbot) -> None:
    page = TaskPage()
    controller = TaskController(page)
    qtbot.addWidget(page)
    task = build_task_record()
    controller.load_queue((task,))

    with qtbot.waitSignal(controller.workspace_state_changed) as signal:
        controller.publish(TransferEvent(task.id, TaskState.WAITING_FOR_NETWORK))

    assert signal.args == [TaskState.WAITING_FOR_NETWORK]


def test_queue_reorder_is_persisted_as_complete_permutation(qtbot) -> None:
    page = TaskPage()

    class Repository:
        def __init__(self) -> None:
            self.orders: list[tuple[TaskId, ...]] = []

        def reorder_queued_tasks(self, task_ids: tuple[TaskId, ...]) -> None:
            self.orders.append(task_ids)

    repository = Repository()
    controller = TaskController(page, repository=repository)
    qtbot.addWidget(page)
    first = replace(build_task_record(), state=TaskState.QUEUED)
    second = type(first)(
        **{**{field: getattr(first, field) for field in first.__dataclass_fields__}, "id": TaskId("task-2"), "name": "Second"}
    )

    controller.load_queue((first, second))
    page.queue_list.setCurrentRow(1)
    page.move_up_button.click()

    assert repository.orders == [(second.id, first.id)]
    assert page.selected_task_id() == second.id


def test_appending_created_task_preserves_existing_queue(qtbot) -> None:
    page = TaskPage()
    controller = TaskController(page)
    qtbot.addWidget(page)
    first = build_task_record()
    second = type(first)(
        **{
            **{field: getattr(first, field) for field in first.__dataclass_fields__},
            "id": TaskId("task-2"),
            "name": "Second",
        }
    )

    controller.load_queue((first,))
    controller.append_task(second)

    assert page.queue_list.count() == 2
    assert page.queue_list.item(0).data(Qt.ItemDataRole.UserRole) == first.id
    assert page.queue_list.item(1).data(Qt.ItemDataRole.UserRole) == second.id


def test_progress_update_keeps_copy_and_verification_separate(qtbot) -> None:
    page = TaskPage()
    controller = TaskController(page)
    qtbot.addWidget(page)

    controller.publish_progress(snapshot(copy_percent=100, verify_percent=25))

    qtbot.waitUntil(lambda: page.verify_progress.value() == 25)
    assert page.copy_progress.value() == 100
    assert page.status_label.text() == "正在校验"


def test_task_buttons_follow_current_task_state(qtbot) -> None:
    page = TaskPage()
    qtbot.addWidget(page)

    page.apply_event(TransferEvent(TaskId("task-1"), TaskState.RUNNING))
    assert page.pause_button.isEnabled() is True
    assert page.resume_button.isEnabled() is False
    assert page.cancel_button.isEnabled() is True

    page.apply_event(TransferEvent(TaskId("task-1"), TaskState.PAUSED))
    assert page.pause_button.isEnabled() is False
    assert page.resume_button.isEnabled() is True
    assert page.cancel_button.isEnabled() is True

    page.show_result(TaskResult(True, TaskState.COMPLETED))
    assert page.pause_button.isEnabled() is False
    assert page.resume_button.isEnabled() is False
    assert page.cancel_button.isEnabled() is False


def test_exported_summary_omits_technical_error_details(qtbot, tmp_path: Path) -> None:
    page = TaskPage()
    qtbot.addWidget(page)
    page.status_label.setText("失败")
    page.result_summary.setText("迁移失败")
    page.error_details.setPlainText("password=secret NAS path /private/share")
    destination = tmp_path / "report.txt"

    page.export_summary(destination)

    content = destination.read_text(encoding="utf-8")
    assert "状态：失败" in content
    assert "结果：迁移失败" in content
    assert "secret" not in content
    assert "/private/share" not in content


def test_missing_local_source_is_readable_and_stays_an_item_level_failure(qtbot) -> None:
    page = TaskPage()
    qtbot.addWidget(page)
    recorded: list[tuple[object, str]] = []

    class Repository:
        def record_ui_error(self, task_id, code) -> None:
            recorded.append((task_id, code))

    controller = TaskController(page, repository=Repository())
    task = replace(build_task_record(), state=TaskState.QUEUED)

    controller.load_queue((task,))
    page.queue_list.setCurrentRow(0)
    controller.publish_result(
        TaskResult(
            False,
            TaskState.FAILED,
            error=SourceFileMissingError(errno.ENOENT, "source file is missing"),
            task_id=task.id,
        )
    )

    assert recorded == [(task.id, "source_not_found")]
    assert "已被此前的任务处理" in page.result_summary.text()
    assert page.status_label.text() != "执行已停止 · 需要处理"


def test_reloaded_item_level_failure_keeps_its_readable_reason(qtbot) -> None:
    page = TaskPage()
    qtbot.addWidget(page)

    class Repository:
        def last_ui_error(self, task_id) -> str:
            return "source_not_found"

    controller = TaskController(page, repository=Repository())
    task = replace(build_task_record(), state=TaskState.FAILED)

    controller.load_queue((task,))
    page.queue_list.setCurrentRow(0)

    assert "已被此前的任务处理" in page.result_summary.text()
    assert page.status_label.text() != "执行已停止 · 需要处理"


def test_reloaded_execution_error_still_stops_the_task(qtbot) -> None:
    page = TaskPage()
    qtbot.addWidget(page)

    class Repository:
        def last_ui_error(self, task_id) -> str:
            return "database_thread_error"

    controller = TaskController(page, repository=Repository())
    task = replace(build_task_record(), state=TaskState.RUNNING)

    controller.load_queue((task,))
    page.queue_list.setCurrentRow(0)

    assert page.status_label.text() == "执行已停止 · 需要处理"


def test_command_failure_on_a_paused_task_keeps_the_task_level_stop(qtbot) -> None:
    # A resume/cancel command can fail while the row still holds its previous
    # TaskState; re-selecting it must not downgrade the stop message, because
    # that message carries the "some files may already have been transferred"
    # safety prompt.
    page = TaskPage()
    qtbot.addWidget(page)

    class FailingResume:
        def resume(self, task_id: object) -> None:
            raise OSError(errno.EIO, "resume rejected")

    controller = TaskController(page, commands=FailingResume())
    task = replace(build_task_record(), state=TaskState.PAUSED)

    controller.load_queue((task,))
    page.queue_list.setCurrentRow(0)
    page.resume_requested.emit()
    page.selection_changed.emit(task.id)

    assert page.status_label.text() == "执行已停止 · 需要处理"
    assert "任务已停止" in page.safety_label.text()


def test_gateway_missing_source_is_persisted_as_source_not_found(qtbot, item_worker_fixture) -> None:
    # End-to-end composition chain: the local gateway's typed missing-source
    # error must survive the item worker and be persisted as ``source_not_found``
    # rather than degenerating into the remote ``path_not_found``.
    fixture = item_worker_fixture
    fixture.local.source_exists = False

    outcome = fixture.worker.run(fixture.item, fixture.task, CancellationToken())

    assert isinstance(outcome.error, SourceFileMissingError)
    recorded: list[tuple[object, str]] = []

    class Repository:
        def record_ui_error(self, task_id, code) -> None:
            recorded.append((task_id, code))

    page = TaskPage()
    qtbot.addWidget(page)
    controller = TaskController(page, repository=Repository())
    controller.load_queue((fixture.task,))
    controller.publish_result(
        TaskResult(False, TaskState.FAILED, error=outcome.error, task_id=fixture.task.id)
    )

    assert recorded == [(fixture.task.id, "source_not_found")]


def test_exported_summary_includes_the_deletion_outcome(qtbot, tmp_path: Path) -> None:
    page = TaskPage()
    qtbot.addWidget(page)

    class Repository:
        def last_deletion_outcome_for_task(self, task_id):
            return (
                "source_missing_after_move",
                "source was absent: OSError(5, '/private/share/source.bin')",
            )

    controller = TaskController(page, repository=Repository())
    task = replace(build_task_record(), state=TaskState.COMPLETED)

    controller.load_queue((task,))
    page.queue_list.setCurrentRow(0)
    destination = tmp_path / "report.txt"
    page.export_summary(destination)
    content = destination.read_text(encoding="utf-8")

    assert "源文件处理" in content
    assert "source_missing_after_move" in content
    assert "/private/share" not in content
    assert "OSError" not in content
