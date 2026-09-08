from dataclasses import replace

from nasmove.core.states import TaskState
from nasmove.ui.main_window import MainWindow
from tests.fixtures.builders import build_task_record


def test_main_window_navigation_reaches_each_transfer_step(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    assert "1  连接 NAS" in window.step_label.text()
    assert window.page_title_label.text() == "连接 NAS"

    assert window.pages.currentWidget() is window.connection_page
    window.next_button.click()
    assert window.pages.currentWidget() is window.source_page
    assert window.page_title_label.text() == "选择本地文件"
    window.next_button.click()
    assert window.pages.currentWidget() is window.target_page
    window.next_button.click()
    assert window.pages.currentWidget() is window.task_page
    assert window.next_button.isEnabled() is False

    window.back_button.click()
    assert window.pages.currentWidget() is window.target_page


def test_main_window_wires_task_page_to_commands(qtbot) -> None:
    class Commands:
        def __init__(self) -> None:
            self.paused: list[object] = []

        def pause(self, task_id: object) -> None:
            self.paused.append(task_id)

    commands = Commands()
    window = MainWindow(task_commands=commands)
    qtbot.addWidget(window)
    task = replace(build_task_record(), state=TaskState.QUEUED)

    window.task_controller.load_queue((task,))
    window.task_page.pause_button.click()

    assert commands.paused == [task.id]


def test_main_window_builds_default_task_commands_from_repository_and_queue(qtbot) -> None:
    class Repository:
        pass

    class Queue:
        pass

    window = MainWindow(task_repository=Repository(), queue_coordinator=Queue())
    qtbot.addWidget(window)

    assert window.task_commands is not None
    assert window.queue_execution is not None
