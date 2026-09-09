from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QObject, QSettings, Signal


class ThemeName(StrEnum):
    SYSTEM = "system"
    FROSTED_LIGHT = "frosted_light"
    MIDNIGHT_OPS = "midnight_ops"
    WARM_STUDIO = "warm_studio"


@dataclass(frozen=True)
class ThemePalette:
    window: str
    surface: str
    surface_border: str
    text: str
    muted_text: str
    primary: str
    primary_hover: str
    selected: str
    progress: str


FROSTED_LIGHT = ThemePalette(
    window="#F4F8FC",
    surface="#FFFFFF",
    surface_border="#C9D9E9",
    text="#18344F",
    muted_text="#637D97",
    primary="#175CD3",
    primary_hover="#0B4DAF",
    selected="#E0EFFF",
    progress="#418BE2",
)

MIDNIGHT_OPS = ThemePalette(
    window="#0C1522",
    surface="#122234",
    surface_border="#2A4966",
    text="#EDF6FF",
    muted_text="#A9C4DC",
    primary="#4A9AFF",
    primary_hover="#75B6FF",
    selected="#1D3B59",
    progress="#45CBDF",
)

WARM_STUDIO = ThemePalette(
    window="#F8F3EB",
    surface="#FFFCF5",
    surface_border="#E2D4C3",
    text="#43382C",
    muted_text="#786B5C",
    primary="#B76035",
    primary_hover="#914722",
    selected="#F9E4D4",
    progress="#CB7545",
)

_PALETTES = {
    ThemeName.SYSTEM: FROSTED_LIGHT,
    ThemeName.FROSTED_LIGHT: FROSTED_LIGHT,
    ThemeName.MIDNIGHT_OPS: MIDNIGHT_OPS,
    ThemeName.WARM_STUDIO: WARM_STUDIO,
}


class ThemeController(QObject):
    theme_changed = Signal(object)

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self._settings = settings
        self._selected = self._saved_theme()

    def selected_theme(self) -> ThemeName:
        return self._selected

    def select(self, theme: ThemeName) -> None:
        self._selected = theme
        self._settings.setValue("appearance/theme", theme.value)
        self._settings.sync()
        self.theme_changed.emit(theme)

    def stylesheet(self) -> str:
        palette = _PALETTES[self._selected]
        return f"""
            QMainWindow, QWidget {{
                background: {palette.window};
                color: {palette.text};
            }}
            QWidget[themeRole="surface"] {{
                background: {palette.surface};
                border: 1px solid {palette.surface_border};
                border-radius: 12px;
            }}
            QLabel[themeRole="muted"] {{ color: {palette.muted_text}; }}
            QPushButton[themeRole="primary"] {{
                background: {palette.primary};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 7px 12px;
            }}
            QPushButton[themeRole="primary"]:hover {{ background: {palette.primary_hover}; }}
            QPushButton[themeRole="selected"] {{ background: {palette.selected}; }}
            QProgressBar::chunk {{ background: {palette.progress}; }}
            QLabel[taskState="running"] {{ color: #175CD3; }}
            QLabel[taskState="recovered"] {{ color: #067647; }}
            QLabel[taskState="warning"] {{ color: #B54708; }}
            QLabel[taskState="failure"] {{ color: #B42318; }}
        """

    def _saved_theme(self) -> ThemeName:
        value = str(self._settings.value("appearance/theme", ThemeName.SYSTEM.value))
        try:
            return ThemeName(value)
        except ValueError:
            return ThemeName.SYSTEM


__all__ = ["ThemeController", "ThemeName", "ThemePalette"]
