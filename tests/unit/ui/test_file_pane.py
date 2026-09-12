from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QHeaderView

from nasmove.ui.directory_models import (
    DirectoryEntryViewModel,
    DirectorySide,
    DirectorySnapshot,
)
from nasmove.ui.file_pane import FilePane


def _snapshot() -> DirectorySnapshot:
    return DirectorySnapshot(
        side=DirectorySide.LOCAL,
        location="/tmp",
        entries=(
            DirectoryEntryViewModel("Album", True, 0, None, "/tmp/Album"),
            DirectoryEntryViewModel("movie.mov", False, 20, None, "/tmp/movie.mov"),
        ),
        request_id=1,
        profile_id=None,
    )


def test_file_pane_selects_files_and_activates_directories(qtbot) -> None:
    pane = FilePane(DirectorySide.LOCAL, "Mac")
    qtbot.addWidget(pane)
    assert pane.table.wordWrap() is False
    pane.apply_snapshot(_snapshot())

    with qtbot.waitSignal(pane.directory_requested) as signal:
        pane.table.doubleClicked.emit(pane.model.index(0, 0))
    assert signal.args[0].name == "Album"

    pane.table.selectRow(1)
    assert pane.selected_entries()[0].name == "movie.mov"


def test_file_pane_filters_without_losing_location(qtbot) -> None:
    pane = FilePane(DirectorySide.LOCAL, "Mac")
    qtbot.addWidget(pane)
    pane.apply_snapshot(_snapshot())
    pane.search_edit.setText("movie")
    qtbot.waitUntil(lambda: pane.model.rowCount() == 1)

    assert pane.model.rowCount() == 1
    assert pane.location == "/tmp"


def test_file_pane_uses_a_borderless_file_browser_table(qtbot) -> None:
    pane = FilePane(DirectorySide.LOCAL, "Mac")
    qtbot.addWidget(pane)

    header = pane.table.horizontalHeader()

    assert pane.table.showGrid() is False
    assert pane.table.objectName() == "fileTable"
    assert header.sectionResizeMode(0) is QHeaderView.ResizeMode.Stretch
    assert header.sectionResizeMode(1) is QHeaderView.ResizeMode.Fixed
    assert header.sectionResizeMode(2) is QHeaderView.ResizeMode.Fixed


def test_file_pane_search_clears_selection_and_updates_empty_state(qtbot) -> None:
    pane = FilePane(DirectorySide.LOCAL, "Mac")
    qtbot.addWidget(pane)
    pane.show()
    pane.apply_snapshot(_snapshot())
    pane.table.selectRow(1)
    assert len(pane.selected_entries()) == 1

    # Search with no matching items
    pane.search_edit.setText("non_existent_file")
    qtbot.waitUntil(lambda: pane.model.rowCount() == 0)
    assert pane.model.rowCount() == 0
    assert pane.selected_entries() == ()
    assert not pane.empty_label.isHidden()
    assert pane.empty_label.text() == "没有匹配结果"
    assert pane.table.isHidden()

    # Clear search
    pane.search_edit.setText("")
    assert pane.model.rowCount() == 2
    assert pane.empty_label.isHidden()
    assert not pane.table.isHidden()


def test_file_pane_connection_guidance_for_remote(qtbot) -> None:
    pane = FilePane(DirectorySide.REMOTE, "NAS")
    qtbot.addWidget(pane)
    pane.show()
    pane.set_connection_guidance(True)

    assert not pane.empty_label.isHidden()
    assert "未连接" in pane.empty_label.text()
    assert not pane.connect_button.isHidden()
    assert pane.table.isHidden()

    with qtbot.waitSignal(pane.connect_requested):
        pane.connect_button.click()

    pane.set_connection_guidance(False)
    assert pane.connect_button.isHidden()


def test_file_pane_responsive_column_hiding(qtbot) -> None:
    pane = FilePane(DirectorySide.LOCAL, "Mac")
    qtbot.addWidget(pane)
    pane.apply_snapshot(_snapshot())

    pane.resize(250, 400)
    pane._update_column_visibility()
    assert pane.table.isColumnHidden(2) is True

    pane.resize(400, 400)
    pane._update_column_visibility()
    assert pane.table.isColumnHidden(2) is False


def test_file_pane_sorting_folders_first_and_columns(qtbot) -> None:
    snapshot = DirectorySnapshot(
        side=DirectorySide.LOCAL,
        location="/test",
        entries=(
            DirectoryEntryViewModel("zebra", True, 0, 100, "/test/zebra"),
            DirectoryEntryViewModel("apple", True, 0, 200, "/test/apple"),
            DirectoryEntryViewModel("beta.txt", False, 2048, 50, "/test/beta.txt"),
            DirectoryEntryViewModel("alpha.txt", False, 1024, 300, "/test/alpha.txt"),
        ),
        request_id=1,
        profile_id=None,
    )
    pane = FilePane(DirectorySide.LOCAL, "Mac")
    qtbot.addWidget(pane)
    pane.apply_snapshot(snapshot)

    # Default name ascending: folders first ("apple", "zebra"), then files ("alpha.txt", "beta.txt")
    names = [pane.model.entry_at(i).name for i in range(4)]
    assert names == ["apple", "zebra", "alpha.txt", "beta.txt"]

    # Name descending: folders first ("zebra", "apple"), then files ("beta.txt", "alpha.txt")
    pane.model.sort(0, Qt.SortOrder.DescendingOrder)
    names = [pane.model.entry_at(i).name for i in range(4)]
    assert names == ["zebra", "apple", "beta.txt", "alpha.txt"]

    # Size ascending (numeric! 1024 before 2048): folders first, then alpha.txt, beta.txt
    pane.model.sort(1, Qt.SortOrder.AscendingOrder)
    names = [pane.model.entry_at(i).name for i in range(4)]
    assert names[:2] == ["apple", "zebra"]
    assert names[2:] == ["alpha.txt", "beta.txt"]

    # Size descending: folders first, then beta.txt (2048), alpha.txt (1024)
    pane.model.sort(1, Qt.SortOrder.DescendingOrder)
    names = [pane.model.entry_at(i).name for i in range(4)]
    assert names[2:] == ["beta.txt", "alpha.txt"]

    # Modified time ascending: folders first, then beta.txt (50), alpha.txt (300)
    pane.model.sort(2, Qt.SortOrder.AscendingOrder)
    names = [pane.model.entry_at(i).name for i in range(4)]
    assert names[2:] == ["beta.txt", "alpha.txt"]


def test_file_pane_refresh_and_new_folder(qtbot) -> None:
    pane = FilePane(DirectorySide.REMOTE, "NAS")
    qtbot.addWidget(pane)

    with qtbot.waitSignal(pane.refresh_requested):
        pane.refresh_button.click()

    received = []
    pane.new_folder_requested.connect(received.append)
    pane.new_folder_requested.emit("NewDir")
    assert received == ["NewDir"]


def test_file_pane_keyboard_navigation(qtbot) -> None:
    pane = FilePane(DirectorySide.LOCAL, "Mac")
    qtbot.addWidget(pane)
    pane.apply_snapshot(_snapshot())
    pane.table.selectRow(0)

    # Enter on folder emits directory_requested
    with qtbot.waitSignal(pane.directory_requested) as signal:
        qtbot.keyClick(pane.table, Qt.Key.Key_Return)
    assert signal.args[0].name == "Album"

    # Backspace on table emits parent_requested
    with qtbot.waitSignal(pane.parent_requested):
        qtbot.keyClick(pane.table, Qt.Key.Key_Backspace)


def test_file_pane_drag_and_drop_local_directory(qtbot, tmp_path) -> None:
    pane = FilePane(DirectorySide.LOCAL, "Mac")
    qtbot.addWidget(pane)
    target_dir = tmp_path / "drag_target"
    target_dir.mkdir()

    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(target_dir))])
    event = QDropEvent(
        QPointF(10.0, 10.0),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    with qtbot.waitSignal(pane.directory_requested) as signal:
        pane.dropEvent(event)
    assert signal.args[0].name == "drag_target"
