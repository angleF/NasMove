import pytest

from nasmove.core.errors import InvalidRemotePath
from nasmove.core.model import RemotePath
from nasmove.core.ports import RemoteEntry
from nasmove.ui.target_page import TargetPage
from nasmove.ui.view_models import RemoteBrowseReport


def test_target_must_be_selected_from_a_non_root_remote_directory(qtbot) -> None:
    page = TargetPage()
    qtbot.addWidget(page)

    assert not hasattr(page, "path_lineedit")
    assert page.add_to_queue_button.isEnabled() is False
    with pytest.raises(InvalidRemotePath, match="请先选择 NAS 目标目录"):
        page.target_path()

    page.show_directory(
        RemoteBrowseReport(
            RemotePath("照片/2026"),
            (RemoteEntry("一月", True, 0),),
            1024,
        )
    )
    page.choose_current_button.click()

    assert page.target_path() == RemotePath("照片/2026")
    assert page.selected_path_label.text() == "照片/2026"
    assert page.add_to_queue_button.isEnabled() is True


def test_share_root_is_for_navigation_and_cannot_be_selected(qtbot) -> None:
    page = TargetPage()
    qtbot.addWidget(page)

    page.show_directory(
        RemoteBrowseReport(None, (RemoteEntry("照片", True, 0),), 2048)
    )

    assert page.location_label.text() == "共享目录根目录"
    assert page.choose_current_button.isEnabled() is False
    assert page.entries_list.item(0).text().endswith("照片")
    assert page.entries_list.item(0).data(256) == "照片"
