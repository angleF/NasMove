from pathlib import Path
from types import SimpleNamespace

from nasmove.core.states import TaskState
from nasmove.ui.directory_models import DirectoryEntryViewModel, DirectorySide, DirectorySnapshot
from nasmove.ui.main_window import MainWindow
from nasmove.ui.navigation import AppDestination
from tests.fixtures.builders import build_connection_config
from tests.fixtures.ui import FakeConnectionService, FakeCredentialStore


def connected_window(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.connection_page.report_ready.emit(SimpleNamespace(success=True))
    return window


def test_connection_success_exposes_file_actions_and_hides_account_form(qtbot):
    window = connected_window(qtbot)
    window.show()
    assert window.transfer_workspace.local_pane.isVisible()
    assert window.transfer_workspace.remote_pane.isVisible()
    assert not window.connection_page.isVisible()
    assert window.transfer_workspace.upload_button.text() == "上传 →"
    assert not window.transfer_workspace.upload_button.isEnabled()
    assert window.transfer_workspace.selection_label.text() == "已选 0 项"


def test_move_is_explicit_and_current_remote_folder_is_the_target(qtbot):
    window = connected_window(qtbot)
    window.transfer_workspace.local_pane.apply_snapshot(
        DirectorySnapshot(
            DirectorySide.LOCAL,
            "/tmp",
            (DirectoryEntryViewModel("example.txt", False, 10, None, "/tmp/example.txt"),),
            1,
            None,
        )
    )
    window.transfer_workspace.local_pane.table.selectRow(0)
    window.transfer_workspace.set_remote_location("archive")

    assert window.transfer_workspace.move_button.isEnabled()
    window.transfer_workspace.move_button.click()
    assert window.source_page.move_checkbox.isChecked()
    assert window.source_page.sources == [Path("/tmp/example.txt")]


def test_changing_account_invalidates_connection_and_old_target(qtbot):
    window = connected_window(qtbot)
    window.source_page.set_sources([Path("/tmp/example.txt")])
    window.target_page.set_selected_path("archive")
    window.show_destination(AppDestination.CONNECTIONS)
    window.connection_page.username_lineedit.setText("another-account")
    assert not window.transfer_workspace.upload_button.isEnabled()
    assert window.transfer_workspace._remote_location is None
    assert window.source_page.sources == [Path("/tmp/example.txt")]


def test_background_running_event_does_not_take_over_new_task(qtbot):
    window = connected_window(qtbot)
    window._show_task_workspace(TaskState.RUNNING)
    assert window.content_stack.currentWidget() is window.transfer_workspace


def test_planning_disables_repeat_submission_and_restores_action(qtbot):
    window = connected_window(qtbot)
    window.transfer_workspace.set_creating(True)
    assert not window.transfer_workspace.upload_button.isEnabled()
    assert window.transfer_workspace.upload_button.text() == "正在创建任务…"
    window.transfer_workspace.set_creating(False)
    assert window.transfer_workspace.upload_button.text() == "上传 →"


def test_saved_account_reconnects_in_background_without_showing_configuration(qtbot):
    config = build_connection_config()
    repository = SimpleNamespace(last_successful_connection=lambda: config)
    credentials = FakeCredentialStore()
    credentials.passwords[str(config.profile_id)] = "test-password"
    tester = FakeConnectionService()
    window = MainWindow(task_repository=repository, credential_store=credentials, tester=tester)
    qtbot.addWidget(window)
    assert window.content_stack.currentWidget() is window.transfer_workspace
    qtbot.waitUntil(lambda: window.setup_workspace.connection_verified)
    qtbot.waitUntil(lambda: window.transfer_workspace._connection_verified)
    qtbot.waitUntil(lambda: window.connection_page._thread is None)
    assert len(tester.requests) == 1
    assert tester.requests[0].config.host == config.host
    assert window.content_stack.currentWidget() is window.transfer_workspace
