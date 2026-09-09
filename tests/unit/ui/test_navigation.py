from nasmove.ui.navigation import AppDestination, AppNavigation


def test_navigation_emits_explicit_destination(qtbot) -> None:
    navigation = AppNavigation()
    qtbot.addWidget(navigation)

    with qtbot.waitSignal(navigation.destination_requested) as signal:
        navigation.buttons[AppDestination.CONNECTIONS].click()

    assert signal.args == [AppDestination.CONNECTIONS]


def test_activity_pill_is_hidden_without_active_tasks_and_opens_queue(qtbot) -> None:
    navigation = AppNavigation()
    qtbot.addWidget(navigation)

    navigation.set_active_task_count(0)
    assert navigation.activity_button.isHidden()

    navigation.set_active_task_count(2)
    assert navigation.activity_button.text() == "2 个任务正在处理"
    with qtbot.waitSignal(navigation.activity_requested):
        navigation.activity_button.click()


def test_selected_destination_is_exposed_on_its_button(qtbot) -> None:
    navigation = AppNavigation()
    qtbot.addWidget(navigation)

    navigation.set_selected(AppDestination.PREFERENCES)

    assert navigation.buttons[AppDestination.PREFERENCES].property("themeRole") == "selected"
    assert navigation.buttons[AppDestination.WORKBENCH].property("themeRole") == ""
