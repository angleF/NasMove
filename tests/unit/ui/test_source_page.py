from pathlib import Path

from PySide6.QtCore import Qt

from nasmove.ui.source_page import SourcePage


def test_selected_sources_can_be_removed_or_cleared(qtbot, tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    page = SourcePage()
    qtbot.addWidget(page)
    page.set_sources([first, second])

    page.list_widget.setCurrentRow(0)
    page.remove_button.click()

    assert page.sources == [second]
    assert page.list_widget.count() == 1
    page.clear_button.click()
    assert page.sources == []
    assert page.list_widget.count() == 0


def test_adding_sources_preserves_existing_selection_and_removes_duplicates(
    qtbot, tmp_path: Path
) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    page = SourcePage()
    qtbot.addWidget(page)
    page.set_sources([first])

    page.add_sources([second, first])

    assert page.sources == [first, second]
    assert page.add_files_button.text() == "添加文件"
    assert page.add_directory_button.text() == "添加文件夹"


def test_delete_key_removes_selected_source(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    page = SourcePage()
    qtbot.addWidget(page)
    page.set_sources([source])
    page.list_widget.setCurrentRow(0)

    qtbot.keyClick(page.list_widget, Qt.Key.Key_Delete)

    assert page.sources == []
