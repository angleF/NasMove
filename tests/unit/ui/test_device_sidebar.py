from dataclasses import replace

from nasmove.core.model import ConnectionProfileId
from nasmove.ui.device_sidebar import DeviceSidebar
from tests.fixtures.builders import build_connection_config


def test_device_sidebar_shows_only_the_current_smb_profile(qtbot) -> None:
    sidebar = DeviceSidebar()
    qtbot.addWidget(sidebar)
    config = build_connection_config()

    sidebar.show_profile(config)
    sidebar.show_connection_state(online=True, latency_ms=3)
    sidebar.show_capacity(1024**4)

    assert sidebar.profile_count() == 1
    assert sidebar.name_label.text() == config.display_name
    assert sidebar.protocol_label.text() == f"SMB · {config.host}"
    assert "已连接" in sidebar.status_label.text()
    assert "TB" in sidebar.capacity_label.text()

    sidebar.set_compact(True)
    assert sidebar.name_label.text() == "NAS"
    assert sidebar.edit_button.text() == "⚙"
    sidebar.set_compact(False)
    assert sidebar.name_label.text() == config.display_name
    assert sidebar.edit_button.text() == "编辑配置"


def test_device_sidebar_lists_and_selects_multiple_profiles(qtbot) -> None:
    sidebar = DeviceSidebar()
    qtbot.addWidget(sidebar)
    home = build_connection_config()
    office = replace(
        home,
        profile_id=ConnectionProfileId("office"),
        display_name="办公 NAS",
        host="nas.office",
    )

    sidebar.set_profiles((home, office), selected_profile_id=office.profile_id)

    assert sidebar.profile_count() == 2
    assert sidebar.current_profile_id() == office.profile_id
    assert sidebar.name_label.text() == office.display_name
    with qtbot.waitSignal(sidebar.profile_selected) as selected:
        sidebar.profile_list.setCurrentRow(0)
    assert selected.args == [home.profile_id]


def test_device_sidebar_emits_profile_management_intents(qtbot) -> None:
    sidebar = DeviceSidebar()
    qtbot.addWidget(sidebar)
    config = build_connection_config()
    sidebar.set_profiles((config,), selected_profile_id=config.profile_id)

    with qtbot.waitSignal(sidebar.add_requested):
        sidebar.add_button.click()
    with qtbot.waitSignal(sidebar.edit_requested):
        sidebar.edit_button.click()
    with qtbot.waitSignal(sidebar.remove_requested) as removed:
        sidebar.remove_button.click()
    assert removed.args == [config.profile_id]


def test_device_sidebar_disables_profile_actions_and_compacts_list(qtbot) -> None:
    sidebar = DeviceSidebar()
    qtbot.addWidget(sidebar)
    config = build_connection_config()
    sidebar.set_profiles((config,), selected_profile_id=config.profile_id)

    sidebar.set_profile_actions_enabled(False)

    assert not sidebar.profile_list.isEnabled()
    assert not sidebar.add_button.isEnabled()
    assert not sidebar.edit_button.isEnabled()
    assert not sidebar.remove_button.isEnabled()

    sidebar.set_compact(True)
    assert sidebar.profile_list.isHidden()
    assert not sidebar.add_button.isHidden()
    assert sidebar.add_button.text() == "＋"
    assert sidebar.remove_button.isHidden()


def test_device_sidebar_compact_mode_keeps_add_button_enabled_without_profiles(qtbot) -> None:
    sidebar = DeviceSidebar()
    qtbot.addWidget(sidebar)

    # Empty profiles (first run) in compact mode
    sidebar.set_compact(True)
    assert not sidebar.add_button.isHidden()
    assert sidebar.add_button.isEnabled() is True
    assert sidebar.add_button.text() == "＋"

    with qtbot.waitSignal(sidebar.add_requested):
        sidebar.add_button.click()
