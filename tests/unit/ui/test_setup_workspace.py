from pathlib import Path

from nasmove.ui.connection_page import ConnectionPage
from nasmove.ui.setup_workspace import SetupWorkspace
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage


def test_setup_workspace_enables_queue_only_after_three_ready_conditions(qtbot) -> None:
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    workspace = SetupWorkspace(connection, sources, target)
    qtbot.addWidget(workspace)

    assert workspace.create_button.isEnabled() is False

    workspace.set_connection_verified(True)
    sources.set_sources([Path("/tmp/source")])
    target.set_selected_path("archive/2026")

    assert workspace.create_button.isEnabled() is True
    assert "archive/2026" in workspace.target_summary.text()


def test_setup_workspace_describes_move_as_verify_commit_then_delete(qtbot) -> None:
    workspace = SetupWorkspace(ConnectionPage(), SourcePage(), TargetPage())
    qtbot.addWidget(workspace)

    workspace.source_page.move_checkbox.setChecked(True)

    assert "完整回读" in workspace.safety_label.text()
    assert "移入废纸篓" in workspace.safety_label.text()


def test_setup_workspace_routes_primary_action_through_existing_target_signal(qtbot) -> None:
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    workspace = SetupWorkspace(connection, sources, target)
    qtbot.addWidget(workspace)
    workspace.set_connection_verified(True)
    sources.set_sources([Path("/tmp/source")])
    target.set_selected_path("archive/2026")

    with qtbot.waitSignal(target.create_task_requested):
        workspace.create_button.click()
