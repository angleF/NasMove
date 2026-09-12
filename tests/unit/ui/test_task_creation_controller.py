from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from nasmove.core.model import TaskId
from nasmove.core.states import (
    ConflictPolicy,
    TaskState,
    TransferAction,
    VerificationPolicy,
)
from nasmove.ui.connection_page import ConnectionPage
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage
from nasmove.ui.task_creation_controller import TaskCreationController


class Planner:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def plan(self, request: object) -> object:
        self.requests.append(request)
        return SimpleNamespace(task=SimpleNamespace(id=TaskId("planned-task")))


class PreflightPlanner:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.confirmed: list[object] = []
        self.cancelled: list[object] = []

    def preflight(self, request: object, **_kwargs: object) -> object:
        self.requests.append(request)
        summary = SimpleNamespace(
            task=SimpleNamespace(
                id=TaskId("planned-task"),
                total_files=1,
                total_bytes=7,
            ),
            item_count=1,
            total_bytes=7,
            safety_margin=1 << 30,
            required_space=(1 << 30) + 7,
            free_space=1 << 40,
            conflict_count=0,
        )
        return SimpleNamespace(request=request, summary=summary)

    def confirm_preflight(self, session: object) -> object:
        self.confirmed.append(session)
        return session.summary

    def cancel_preflight(self, session: object) -> None:
        self.cancelled.append(session)


class Repository:
    def __init__(self) -> None:
        self.transitions: list[tuple[object, TaskState, TaskState]] = []
        self.tasks: list[object] = []

    def transition_task(self, task_id: object, expected: TaskState, target: TaskState) -> None:
        self.transitions.append((task_id, expected, target))

    def list_incomplete_tasks(self) -> list[object]:
        return self.tasks


class Application:
    def __init__(self) -> None:
        self.enqueued: list[object] = []

    def enqueue(self, task_id: object) -> None:
        self.enqueued.append(task_id)


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No])
def test_real_move_dialog_result(qtbot, answer):
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    for page in (connection, sources, target):
        qtbot.addWidget(page)
    controller = TaskCreationController(
        connection, sources, target,
        planner=Planner(), repository=Repository(), application=Application(),
    )

    def respond():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QMessageBox)
        button = dialog.button(answer)
        button.click()

    QTimer.singleShot(50, respond)
    assert controller._confirm_move_dialog() == (answer == QMessageBox.StandardButton.Yes)


def test_create_move_task_plans_persists_then_enqueues(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    for page in (connection, sources, target):
        qtbot.addWidget(page)
    connection.display_name_lineedit.setText("Home NAS")
    connection.address_lineedit.setText("nas.local")
    connection.share_lineedit.setText("media")
    connection.username_lineedit.setText("operator")
    sources.set_sources([source])
    sources.move_checkbox.setChecked(True)
    target.set_selected_path("incoming")
    planner = Planner()
    repository = Repository()
    application = Application()
    controller = TaskCreationController(
        connection,
        sources,
        target,
        planner=planner,
        repository=repository,
        application=application,
    )

    def confirm_dialog():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QMessageBox)
        button = dialog.button(QMessageBox.StandardButton.Yes)
        button.click()

    QTimer.singleShot(50, confirm_dialog)
    target.add_to_queue_button.click()

    qtbot.waitUntil(lambda: application.enqueued == [TaskId("planned-task")])
    request = planner.requests[0]
    assert request.connection.display_name == "Home NAS"
    assert request.sources == (source,)
    assert request.target_root.value == "incoming"
    assert request.action is TransferAction.MOVE
    assert request.verification_policy is VerificationPolicy.FULL
    assert repository.transitions == [
        (TaskId("planned-task"), TaskState.PREFLIGHT, TaskState.QUEUED)
    ]
    assert target.creation_status_label.text() == "任务已加入队列"
    assert controller is not None


def test_declined_move_confirmation_does_not_plan_or_enqueue(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    for page in (connection, sources, target):
        qtbot.addWidget(page)
    sources.set_sources([source])
    sources.move_checkbox.setChecked(True)
    target.set_selected_path("incoming")
    planner = Planner()
    application = Application()
    TaskCreationController(
        connection,
        sources,
        target,
        planner=planner,
        repository=Repository(),
        application=application,
        confirm_move=lambda: False,
    )

    target.add_to_queue_button.click()

    assert planner.requests == []
    assert application.enqueued == []
    assert target.creation_status_label.text() == "已取消创建移动任务"


def test_task_cannot_start_before_target_directory_is_selected(qtbot) -> None:
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    for page in (connection, sources, target):
        qtbot.addWidget(page)
    application = Application()
    TaskCreationController(
        connection,
        sources,
        target,
        planner=Planner(),
        repository=Repository(),
        application=application,
    )

    assert target.add_to_queue_button.isEnabled() is False

    assert application.enqueued == []


def test_new_task_is_appended_after_existing_queue_positions(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    for page in (connection, sources, target):
        qtbot.addWidget(page)
    sources.set_sources([source])
    target.set_selected_path("incoming")
    planner = Planner()
    repository = Repository()
    repository.tasks = [SimpleNamespace(queue_position=2), SimpleNamespace(queue_position=7)]
    application = Application()
    TaskCreationController(
        connection,
        sources,
        target,
        planner=planner,
        repository=repository,
        application=application,
    )

    target.add_to_queue_button.click()

    qtbot.waitUntil(lambda: bool(application.enqueued))
    assert planner.requests[0].queue_position == 8


def test_preflight_is_confirmed_before_task_is_persisted_and_enqueued(
    qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    for page in (connection, sources, target):
        qtbot.addWidget(page)
    sources.set_sources([source])
    target.set_selected_path("incoming")
    planner = PreflightPlanner()
    repository = Repository()
    application = Application()
    TaskCreationController(
        connection,
        sources,
        target,
        planner=planner,
        repository=repository,
        application=application,
        confirm_preflight=lambda _session: True,
    )

    target.add_to_queue_button.click()

    qtbot.waitUntil(lambda: application.enqueued == [TaskId("planned-task")])
    assert len(planner.requests) == 1
    assert len(planner.confirmed) == 1
    assert planner.cancelled == []
    assert repository.transitions == [
        (TaskId("planned-task"), TaskState.PREFLIGHT, TaskState.QUEUED)
    ]


def test_declined_preflight_is_discarded_without_creating_task(
    qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    for page in (connection, sources, target):
        qtbot.addWidget(page)
    sources.set_sources([source])
    target.set_selected_path("incoming")
    planner = PreflightPlanner()
    repository = Repository()
    application = Application()
    TaskCreationController(
        connection,
        sources,
        target,
        planner=planner,
        repository=repository,
        application=application,
        confirm_preflight=lambda _session: False,
    )

    target.add_to_queue_button.click()

    qtbot.waitUntil(lambda: len(planner.cancelled) == 1)
    assert planner.confirmed == []
    assert repository.transitions == []
    assert application.enqueued == []
    assert target.creation_status_label.text() == "已取消预检，任务未创建"


def test_real_preflight_dialog_shows_summary_and_move_semantics(qtbot) -> None:
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    for page in (connection, sources, target):
        qtbot.addWidget(page)
    controller = TaskCreationController(
        connection,
        sources,
        target,
        planner=Planner(),
        repository=Repository(),
        application=Application(),
    )
    session = SimpleNamespace(
        request=SimpleNamespace(
            sources=(Path("/tmp/source.bin"),),
            action=TransferAction.MOVE,
        ),
        summary=SimpleNamespace(
            task=SimpleNamespace(total_files=3, total_bytes=2048),
            item_count=4,
            total_bytes=2048,
            safety_margin=1 << 30,
            required_space=(1 << 30) + 2048,
            free_space=2 << 30,
            conflict_count=2,
        ),
    )

    observed: dict[str, object] = {}

    def inspect_and_cancel() -> None:
        dialog = QApplication.activeModalWidget()
        observed["is_message"] = isinstance(dialog, QMessageBox)
        if not isinstance(dialog, QMessageBox):
            return
        observed["text"] = dialog.text() + "\n" + dialog.informativeText()
        dialog.reject()

    QTimer.singleShot(50, inspect_and_cancel)
    assert controller._confirm_preflight_dialog(session) is False
    assert observed["is_message"] is True
    assert "文件数：3" in str(observed["text"])
    assert "冲突：2" in str(observed["text"])
    assert "移入废纸篓" in str(observed["text"])


def test_selected_conflict_policy_is_included_in_plan_request(
    qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    for page in (connection, sources, target):
        qtbot.addWidget(page)
    sources.set_sources([source])
    target.set_selected_path("incoming")
    planner = Planner()
    application = Application()
    TaskCreationController(
        connection,
        sources,
        target,
        planner=planner,
        repository=Repository(),
        application=application,
        conflict_policy_provider=lambda: ConflictPolicy.SKIP,
    )

    target.add_to_queue_button.click()

    qtbot.waitUntil(lambda: bool(application.enqueued))
    assert planner.requests[0].conflict_policy is ConflictPolicy.SKIP


def test_canceling_ask_policy_does_not_start_preflight(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    for page in (connection, sources, target):
        qtbot.addWidget(page)
    sources.set_sources([source])
    target.set_selected_path("incoming")
    planner = PreflightPlanner()
    TaskCreationController(
        connection,
        sources,
        target,
        planner=planner,
        repository=Repository(),
        application=Application(),
        conflict_policy_provider=lambda: ConflictPolicy.ASK,
        resolve_ask_policy=lambda: None,
    )

    target.add_to_queue_button.click()

    assert planner.requests == []
    assert target.creation_status_label.text() == "已取消选择冲突策略，任务未创建"
