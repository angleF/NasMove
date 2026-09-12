from dataclasses import replace
from types import SimpleNamespace

from PySide6.QtCore import QSettings
from PySide6.QtGui import QCloseEvent

from nasmove.core.errors import ConnectionProfileInUse
from nasmove.core.model import ConnectionProfileId
from nasmove.core.states import TaskState
from nasmove.ui.connection_profile_service import ProfileArchiveResult
from nasmove.ui.directory_models import DirectoryEntryViewModel, DirectorySide, DirectorySnapshot
from nasmove.ui.main_window import MainWindow
from nasmove.ui.navigation import AppDestination
from nasmove.ui.theme import ThemeController, ThemeName
from tests.fixtures.builders import build_connection_config, build_task_record


class ProfileService:
    def __init__(self, profiles) -> None:
        self.profiles = list(profiles)
        self.archived = []
        self.archive_result = ProfileArchiveResult(True, True)
        self.block_archive = False

    def list_profiles(self):
        return tuple(self.profiles)

    def get_profile(self, profile_id):
        return next(profile for profile in self.profiles if profile.profile_id == profile_id)

    def archive(self, profile_id):
        if self.block_archive:
            raise ConnectionProfileInUse(str(profile_id))
        self.archived.append(profile_id)
        self.profiles = [profile for profile in self.profiles if profile.profile_id != profile_id]
        return self.archive_result


class ResetGateway:
    def __init__(self) -> None:
        self.reset_calls = 0

    def reset_connection(self) -> None:
        self.reset_calls += 1


def test_main_window_routes_sidebar_destinations_without_losing_setup_editors(qtbot, tmp_path) -> None:
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)
    window = MainWindow(theme_controller=ThemeController(settings))
    qtbot.addWidget(window)

    assert window.content_stack.currentWidget() is window.transfer_workspace
    window.connection_page.report_ready.emit(SimpleNamespace(success=True))
    assert window.content_stack.currentWidget() is window.transfer_workspace
    window.new_task_button.click()
    assert window.content_stack.currentWidget() is window.transfer_workspace

    window.navigation.buttons[AppDestination.CONNECTIONS].click()
    assert window.content_stack.currentWidget() is window.connection_page

    window.navigation.buttons[AppDestination.QUEUE].click()
    assert window.content_stack.currentWidget() is window.task_page

    window.show_destination(AppDestination.WORKBENCH)
    assert window.content_stack.currentWidget() is window.transfer_workspace


def test_main_window_attaches_queue_and_connection_state_to_transfer_workspace(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    task = replace(build_task_record(), state=TaskState.RUNNING)

    window.task_controller.load_queue((task,))
    assert window.content_stack.currentWidget() is window.transfer_workspace
    window.connection_page.report_ready.emit(SimpleNamespace(success=True))

    assert window.transfer_workspace.queue_panel.row_count() == 1
    assert window.transfer_workspace.device_sidebar.profile_count() == 1
    assert window.transfer_workspace._connection_verified is True


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


def test_switching_profile_invalidates_old_remote_context(qtbot) -> None:
    home = build_connection_config()
    office = replace(
        home,
        profile_id=ConnectionProfileId("office"),
        display_name="办公 NAS",
        host="nas.office",
    )
    profiles = ProfileService((home, office))
    gateway = ResetGateway()
    window = MainWindow(profile_service=profiles, gateway=gateway)
    qtbot.addWidget(window)
    window.transfer_workspace.set_connection_verified(True)
    window.transfer_workspace.apply_directory_snapshot(
        DirectorySnapshot(
            DirectorySide.REMOTE,
            "archive",
            (DirectoryEntryViewModel("old", True, 0, None, "archive/old"),),
            1,
            str(home.profile_id),
        )
    )
    window.transfer_workspace.device_sidebar.show_capacity(1024)

    window._select_profile(office.profile_id)

    assert window.connection_page.profile_id == office.profile_id
    assert window.transfer_workspace._connection_verified is False
    assert window.transfer_workspace.remote_pane.table.model().rowCount() == 0
    assert window.transfer_workspace._remote_location is None
    assert window.transfer_workspace.device_sidebar.capacity_label.text() == "容量尚未查询"
    assert gateway.reset_calls == 1


def test_profile_switch_is_rejected_while_task_creation_is_active(qtbot) -> None:
    home = build_connection_config()
    office = replace(home, profile_id=ConnectionProfileId("office"))
    profiles = ProfileService((home, office))
    window = MainWindow(profile_service=profiles)
    qtbot.addWidget(window)
    window.source_page.setEnabled(False)

    window._select_profile(office.profile_id)

    assert window.connection_page.profile_id == home.profile_id
    assert window.transfer_workspace.device_sidebar.current_profile_id() == home.profile_id
    assert "任务创建中" in window.connection_status_label.text()


def test_removing_profile_refreshes_selection_and_reports_credential_warning(qtbot) -> None:
    home = build_connection_config()
    office = replace(home, profile_id=ConnectionProfileId("office"))
    profiles = ProfileService((home, office))
    profiles.archive_result = ProfileArchiveResult(True, False, "credential_cleanup_failed")
    window = MainWindow(
        profile_service=profiles,
        confirm_profile_removal=lambda _config: True,
    )
    qtbot.addWidget(window)

    window._remove_profile(home.profile_id)

    assert profiles.archived == [home.profile_id]
    assert window.connection_page.profile_id == office.profile_id
    assert window.transfer_workspace.device_sidebar.profile_count() == 1
    assert "本机凭据" in window.connection_status_label.text()


def test_removing_profile_in_use_preserves_selection(qtbot) -> None:
    home = build_connection_config()
    profiles = ProfileService((home,))
    profiles.block_archive = True
    window = MainWindow(
        profile_service=profiles,
        confirm_profile_removal=lambda _config: True,
    )
    qtbot.addWidget(window)

    window._remove_profile(home.profile_id)

    assert window.connection_page.profile_id == home.profile_id
    assert window.transfer_workspace.device_sidebar.profile_count() == 1
    assert "未完成任务" in window.connection_status_label.text()


def test_new_profile_clears_connection_context_and_opens_editor(qtbot) -> None:
    home = build_connection_config()
    window = MainWindow(profile_service=ProfileService((home,)))
    qtbot.addWidget(window)

    window._new_profile()

    assert window.connection_page.profile_id is None
    assert window.content_stack.currentWidget() is window.connection_page
    assert window.transfer_workspace._connection_verified is False


def test_main_window_close_stops_directory_browser(qtbot) -> None:
    class Browser:
        shutdown_calls = 0

        def shutdown(self) -> bool:
            self.shutdown_calls += 1
            return True

    window = MainWindow()
    qtbot.addWidget(window)
    browser = Browser()
    window.directory_browser = browser
    event = QCloseEvent()

    window.closeEvent(event)

    assert browser.shutdown_calls == 1
    assert event.isAccepted()


def test_main_window_close_gates_on_queue_shutdown_completion(qtbot) -> None:
    class Queue:
        def __init__(self) -> None:
            self.calls: list[float] = []

        def shutdown(self, timeout_seconds: float = 2.0) -> bool:
            self.calls.append(timeout_seconds)
            return True

    queue = Queue()
    window = MainWindow(queue_coordinator=queue)
    qtbot.addWidget(window)
    event = QCloseEvent()

    window.closeEvent(event)

    assert queue.calls == [2.0]
    assert event.isAccepted()


def test_main_window_close_is_ignored_while_queue_is_still_stopping(qtbot) -> None:
    class Queue:
        def shutdown(self, timeout_seconds: float = 2.0) -> bool:
            assert timeout_seconds == 2.0
            return False

    window = MainWindow(queue_coordinator=Queue())
    qtbot.addWidget(window)
    event = QCloseEvent()

    window.closeEvent(event)

    assert not event.isAccepted()
    assert "传输" in window.connection_status_label.text()


def test_main_window_close_without_queue_shutdown_contract_is_unaffected(qtbot) -> None:
    class Queue:
        pass

    window = MainWindow(queue_coordinator=Queue())
    qtbot.addWidget(window)
    event = QCloseEvent()

    window.closeEvent(event)

    assert event.isAccepted()


def test_main_window_directory_gate_precedes_queue_gate(qtbot) -> None:
    class Browser:
        def shutdown(self) -> bool:
            return False

    class Queue:
        def __init__(self) -> None:
            self.calls = 0

        def shutdown(self, timeout_seconds: float = 2.0) -> bool:
            self.calls += 1
            return True

    window = MainWindow()
    qtbot.addWidget(window)
    window.directory_browser = Browser()
    queue = Queue()
    window.queue_coordinator = queue
    event = QCloseEvent()

    window.closeEvent(event)

    assert not event.isAccepted()
    assert "目录读取" in window.connection_status_label.text()
    assert queue.calls == 0


def test_main_window_navigation_visible_and_back_returns_to_workbench(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    assert not window.navigation.isHidden()

    window.show_destination(AppDestination.QUEUE)
    assert window.content_stack.currentWidget() is window.task_page

    window.task_page.back_button.click()
    assert window.content_stack.currentWidget() is window.transfer_workspace
    assert window.navigation.buttons[AppDestination.WORKBENCH].property("themeRole") == "selected"


def test_main_window_task_creation_keeps_transfer_workspace(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.content_stack.currentWidget() is window.transfer_workspace

    window._task_created(replace(build_task_record(), state=TaskState.QUEUED))
    assert window.content_stack.currentWidget() is window.transfer_workspace


def test_main_window_wires_file_pane_refresh_and_new_folder(qtbot, tmp_path) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    window.transfer_workspace.local_pane.location = str(tmp_path)
    window.transfer_workspace.local_pane.refresh_button.click()

    window.transfer_workspace.local_pane.new_folder_requested.emit("new_sub")
    assert (tmp_path / "new_sub").is_dir()
