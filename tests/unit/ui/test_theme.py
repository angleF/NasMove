from PySide6.QtCore import QSettings

from nasmove.ui.theme import ThemeController, ThemeName


def test_theme_defaults_to_system_and_uses_frosted_light_fallback(tmp_path) -> None:
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)

    controller = ThemeController(settings)

    assert controller.selected_theme() is ThemeName.SYSTEM
    assert "#175CD3" in controller.stylesheet()


def test_selecting_theme_persists_and_notifies_listeners(tmp_path, qtbot) -> None:
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)
    controller = ThemeController(settings)

    with qtbot.waitSignal(controller.theme_changed) as signal:
        controller.select(ThemeName.MIDNIGHT_OPS)

    assert signal.args == [ThemeName.MIDNIGHT_OPS]
    assert settings.value("appearance/theme") == ThemeName.MIDNIGHT_OPS.value


def test_every_theme_keeps_explicit_failure_and_recovery_tokens(tmp_path) -> None:
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)
    controller = ThemeController(settings)

    for theme in ThemeName:
        controller.select(theme)

        stylesheet = controller.stylesheet()

        assert "#B42318" in stylesheet
        assert "#B54708" in stylesheet


def test_theme_styles_navigation_surfaces_and_form_controls(tmp_path) -> None:
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)

    stylesheet = ThemeController(settings).stylesheet()

    assert "#appNavigation" in stylesheet
    assert 'QPushButton[themeRole="selected"]' in stylesheet
    assert "QLineEdit, QSpinBox, QComboBox" in stylesheet
    assert "#recoveryCard" in stylesheet
