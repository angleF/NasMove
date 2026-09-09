from dataclasses import replace

from PySide6.QtCore import QSettings

from nasmove.core.states import TaskState
from nasmove.ui.main_window import MainWindow
from nasmove.ui.navigation import AppDestination
from nasmove.ui.theme import ThemeController, ThemeName
from tests.fixtures.builders import build_task_record


def test_main_window_routes_sidebar_destinations_without_losing_setup_editors(qtbot, tmp_path) -> None:
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)
    window = MainWindow(theme_controller=ThemeController(settings))
    qtbot.addWidget(window)

    assert window.content_stack.currentWidget() is window.setup_workspace
    window.new_task_button.click()
    assert window.content_stack.currentWidget() is window.setup_workspace

    window.navigation.buttons[AppDestination.CONNECTIONS].click()
    assert window.content_stack.currentWidget() is window.setup_workspace
    assert window.setup_workspace.editor_stack.currentWidget() is window.connection_page

    window.navigation.buttons[AppDestination.QUEUE].click()
    assert window.content_stack.currentWidget() is window.task_page

    window.show_destination(AppDestination.WORKBENCH)
    assert window.content_stack.currentWidget() is window.setup_workspace


def test_main_window_applies_selected_theme(qtbot, tmp_path) -> None:
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)
    controller = ThemeController(settings)
    window = MainWindow(theme_controller=controller)
    qtbot.addWidget(window)

    controller.select(ThemeName.WARM_STUDIO)

    assert "#B76035" in window.styleSheet()


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
