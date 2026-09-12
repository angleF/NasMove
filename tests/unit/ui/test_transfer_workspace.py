from pathlib import Path

from nasmove.core.model import RemotePath
from nasmove.core.states import ConflictPolicy
from nasmove.ui.directory_models import (
    DirectoryEntryViewModel,
    DirectorySide,
    DirectorySnapshot,
)
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage
from nasmove.ui.transfer_workspace import TransferWorkspace


def test_workspace_maps_selection_to_existing_task_draft(qtbot) -> None:
    source_page = SourcePage()
    target_page = TargetPage()
    workspace = TransferWorkspace(source_page, target_page)
    qtbot.addWidget(workspace)
    workspace.set_connection_verified(True)
    workspace.local_pane.apply_snapshot(
        DirectorySnapshot(
            DirectorySide.LOCAL,
            "/tmp",
            (DirectoryEntryViewModel("movie.mov", False, 20, None, "/tmp/movie.mov"),),
            1,
            None,
        )
    )
    workspace.local_pane.table.selectRow(0)
    workspace.set_remote_location("Video")

    with qtbot.waitSignal(workspace.create_task_requested):
        workspace.upload_button.click()

    assert source_page.sources == [Path("/tmp/movie.mov")]
    assert source_page.copy_radio.isChecked()
    assert target_page.target_path() == RemotePath("Video")


def test_workspace_hides_unimplemented_download_and_folds_at_small_width(qtbot) -> None:
    workspace = TransferWorkspace(SourcePage(), TargetPage())
    qtbot.addWidget(workspace)

    assert workspace.pull_button.isHidden()
    workspace.apply_responsive_width(800)
    assert workspace.queue_panel.is_compact is True
    assert workspace.device_sidebar.is_compact is True


def test_workspace_keeps_task_creation_feedback_visible(qtbot) -> None:
    target_page = TargetPage()
    workspace = TransferWorkspace(SourcePage(), target_page)
    qtbot.addWidget(workspace)

    assert workspace.isAncestorOf(target_page.creation_status_label)


def test_workspace_exposes_all_conflict_policies(qtbot) -> None:
    workspace = TransferWorkspace(SourcePage(), TargetPage())
    qtbot.addWidget(workspace)

    values = {
        workspace.policy_selector.itemData(index)
        for index in range(workspace.policy_selector.count())
    }

    assert values == set(ConflictPolicy)
    assert workspace.selected_conflict_policy() is ConflictPolicy.KEEP_BOTH


def test_workspace_uses_narrow_splitter_handles_for_the_three_panel_workbench(qtbot) -> None:
    workspace = TransferWorkspace(SourcePage(), TargetPage())
    qtbot.addWidget(workspace)

    assert workspace.outer_splitter.objectName() == "workbenchSplitter"
    assert workspace.browser_splitter.objectName() == "fileBrowserSplitter"
    assert workspace.outer_splitter.handleWidth() == 6
    assert workspace.browser_splitter.handleWidth() == 6


def test_transfer_workspace_connect_guidance_and_search_selection_integration(qtbot) -> None:
    workspace = TransferWorkspace(SourcePage(), TargetPage())
    qtbot.addWidget(workspace)

    # Initial state: queue is compact by default when 0 tasks
    assert workspace.queue_panel.is_compact is True

    # Remote pane has connection guidance initially
    assert not workspace.remote_pane.connect_button.isHidden()
    assert "未连接" in workspace.remote_pane.empty_label.text()

    # Load local files
    workspace.local_pane.apply_snapshot(
        DirectorySnapshot(
            DirectorySide.LOCAL,
            "/tmp",
            (DirectoryEntryViewModel("doc.pdf", False, 100, None, "/tmp/doc.pdf"),),
            1,
            None,
        )
    )

    # Connect verified
    workspace.set_connection_verified(True)
    assert workspace.remote_pane.connect_button.isHidden()
    workspace.set_remote_location("Backup")

    # Select local file: upload enabled, selection label updated
    workspace.local_pane.table.selectRow(0)
    assert workspace.selection_label.text() == "已选 1 项"
    assert workspace.upload_button.isEnabled() is True

    # Filter with no matching query: selection cleared, label updated, upload button disabled
    workspace.local_pane.search_edit.setText("non_matching")
    qtbot.waitUntil(lambda: len(workspace.local_pane.selected_entries()) == 0)
    assert workspace.local_pane.selected_entries() == ()
    assert workspace.selection_label.text() == "已选 0 项"
    assert workspace.upload_button.isEnabled() is False

    # Clear query: entries restored
    workspace.local_pane.search_edit.setText("")
    assert workspace.local_pane.model.rowCount() == 1


def test_workspace_targets_selected_remote_subdirectory(qtbot) -> None:
    source_page = SourcePage()
    target_page = TargetPage()
    workspace = TransferWorkspace(source_page, target_page)
    qtbot.addWidget(workspace)
    workspace.set_connection_verified(True)
    workspace.local_pane.apply_snapshot(
        DirectorySnapshot(
            DirectorySide.LOCAL,
            "/tmp",
            (DirectoryEntryViewModel("doc.pdf", False, 100, None, "/tmp/doc.pdf"),),
            1,
            None,
        )
    )
    workspace.remote_pane.apply_snapshot(
        DirectorySnapshot(
            DirectorySide.REMOTE,
            "Backup",
            (
                DirectoryEntryViewModel("2026", True, 0, None, "Backup/2026"),
                DirectoryEntryViewModel("notes.txt", False, 10, None, "Backup/notes.txt"),
            ),
            1,
            None,
        )
    )
    workspace.local_pane.table.selectRow(0)
    assert workspace.effective_remote_target() == "Backup"
    assert "Backup" in workspace.target_hint_label.text()

    # Selecting a subdirectory updates effective target and hint label
    workspace.remote_pane.table.selectRow(0)
    assert workspace.effective_remote_target() == "Backup/2026"
    assert "Backup/2026" in workspace.target_hint_label.text()

    # Request create creates task targeting the selected subdirectory
    with qtbot.waitSignal(workspace.create_task_requested):
        workspace.upload_button.click()

    assert target_page.target_path() == RemotePath("Backup/2026")

    # Selecting a file row falls back to the parent directory
    workspace.remote_pane.table.selectRow(1)
    assert workspace.effective_remote_target() == "Backup"

