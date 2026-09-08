from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nasmove.core.model import TaskId
from nasmove.core.states import TaskState, TransferAction, VerificationPolicy
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
        confirm_move=lambda: True,
    )

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
