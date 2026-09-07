from __future__ import annotations

from pathlib import Path
from threading import Thread, get_ident

from PySide6.QtCore import QCoreApplication, Qt

from nasmove.core.model import TaskId
from nasmove.core.states import TaskState
from nasmove.transfer.transfer_engine import TaskResult, TransferEvent
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
    first = build_task_record()
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
