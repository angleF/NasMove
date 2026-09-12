import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen


def _ensure_chevron_icon(color_hex: str) -> str:
    cache_dir = Path(tempfile.gettempdir()) / "nasmove_assets"
    cache_dir.mkdir(parents=True, exist_ok=True)
    slug = color_hex.replace("#", "")
    path = cache_dir / f"chevron_down_{slug}.png"
    if not path.exists():
        img = QImage(24, 24, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(color_hex), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QPointF(7, 9.5), QPointF(12, 14.5), QPointF(17, 9.5)])
        p.end()
        img.save(str(path))
    return str(path)


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
    recovery_bg: str = "#FFF7E5"
    recovery_border: str = "#F2CD7D"
    recovery_text: str = "#B54708"
    status_running: str = "#175CD3"
    status_success: str = "#067647"
    status_warning: str = "#B54708"
    status_danger: str = "#B42318"


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
    recovery_bg="#FFF7E5",
    recovery_border="#F2CD7D",
    recovery_text="#B54708",
    status_running="#175CD3",
    status_success="#067647",
    status_warning="#B54708",
    status_danger="#B42318",
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
    recovery_bg="#2A2210",
    recovery_border="#6E5318",
    recovery_text="#F5C842",
    status_running="#75B6FF",
    status_success="#34D399",
    status_warning="#FBBF24",
    status_danger="#F87171",
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
    recovery_bg="#FFF7E5",
    recovery_border="#E8BE6B",
    recovery_text="#B54708",
    status_running="#B76035",
    status_success="#067647",
    status_warning="#B54708",
    status_danger="#B42318",
)

_PALETTES = {
    ThemeName.SYSTEM: FROSTED_LIGHT,
    ThemeName.FROSTED_LIGHT: FROSTED_LIGHT,
    ThemeName.MIDNIGHT_OPS: MIDNIGHT_OPS,
    ThemeName.WARM_STUDIO: WARM_STUDIO,
}


def is_system_dark() -> bool:
    app = QGuiApplication.instance()
    if app is not None:
        hints = app.styleHints()
        if hints is not None:
            return hints.colorScheme() == Qt.ColorScheme.Dark
    return False


class ThemeController(QObject):
    theme_changed = Signal(object)

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self._settings = settings
        self._selected = self._saved_theme()

    def _on_system_color_scheme_changed(self, _scheme: object = None) -> None:
        if self._selected == ThemeName.SYSTEM:
            self.theme_changed.emit(self._selected)

    def selected_theme(self) -> ThemeName:
        return self._selected

    def select(self, theme: ThemeName) -> None:
        self._selected = theme
        self._settings.setValue("appearance/theme", theme.value)
        self._settings.sync()
        self.theme_changed.emit(theme)

    def palette(self) -> ThemePalette:
        if self._selected == ThemeName.SYSTEM:
            return MIDNIGHT_OPS if is_system_dark() else FROSTED_LIGHT
        return _PALETTES[self._selected]

    def stylesheet(self) -> str:
        palette = self.palette()
        chevron_path = _ensure_chevron_icon(palette.muted_text)
        return f"""
            QMainWindow {{ background: {palette.window}; }}
            QWidget {{
                font-size: 14px;
                background: {palette.window};
                color: {palette.text};
            }}
            QWidget#appNavigation {{
                background: {palette.surface};
                border-right: 1px solid {palette.surface_border};
            }}
            QWidget#transferWorkspace {{ background: {palette.window}; }}
            QWidget#deviceSidebar, QWidget#queuePanel {{
                background: {palette.surface};
                border-color: {palette.surface_border};
            }}
            QWidget#fileBrowserArea {{ background: {palette.surface}; }}
            QLabel#navigationBrand {{
                font-size: 20px;
                font-weight: 700;
                padding: 4px 8px 10px 8px;
            }}
            QLabel#pageTitle, QLabel#workspaceTitle {{
                font-size: 22px;
                font-weight: 700;
            }}
            QWidget[themeRole="surface"] {{
                background: {palette.surface};
                border: 1px solid {palette.surface_border};
                border-radius: 12px;
            }}
            QLabel[themeRole="muted"] {{ color: {palette.muted_text}; }}
            QWidget#appNavigation QPushButton {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 8px;
                color: {palette.text};
                min-height: 30px;
                padding: 5px 10px;
                text-align: left;
            }}
            QWidget#appNavigation QPushButton:hover {{ background: {palette.selected}; }}
            QPushButton {{
                background: {palette.surface};
                border: 1px solid {palette.surface_border};
                border-radius: 6px;
                color: {palette.text};
                min-height: 28px;
                padding: 5px 12px;
                text-align: center;
            }}
            QPushButton:hover {{
                background: {palette.selected};
                border-color: {palette.primary};
                color: {palette.primary};
            }}
            QPushButton[themeRole="secondary"] {{
                background: {palette.surface};
                border: 1px solid {palette.surface_border};
                border-radius: 6px;
                color: {palette.text};
                padding: 6px 14px;
                text-align: center;
                min-height: 28px;
                font-size: 13px;
            }}
            QPushButton[themeRole="secondary"]:hover {{
                background: {palette.selected};
                border-color: {palette.primary};
                color: {palette.primary};
            }}
            QPushButton[themeRole="secondary"]:pressed {{
                background: {palette.surface_border};
            }}
            QPushButton[themeRole="secondary"]:disabled {{
                color: {palette.muted_text};
                border-color: {palette.surface_border};
                background: transparent;
            }}
            QPushButton[themeRole="primary"] {{
                background: {palette.primary};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
                font-weight: 600;
                text-align: center;
                min-height: 32px;
            }}
            QPushButton[themeRole="primary"]:hover {{ background: {palette.primary_hover}; }}
            QPushButton[themeRole="warning"] {{
                background: transparent;
                border: 1px solid {palette.surface_border};
                color: {palette.text};
                text-align: center;
            }}
            QPushButton[themeRole="warning"]:hover {{ background: {palette.selected}; }}
            QPushButton:disabled {{ color: {palette.muted_text}; }}
            QPushButton[themeRole="primary"]:disabled {{
                background: {palette.surface_border};
                color: {palette.muted_text};
            }}
            QPushButton[themeRole="selected"] {{
                background: {palette.selected};
                color: {palette.primary};
                font-weight: 700;
            }}
            QWidget#deviceSidebar QPushButton,
            QPushButton#queueTopButton,
            QPushButton#queuePauseButton,
            QPushButton#queueResumeButton,
            QPushButton#queueCancelButton {{
                background: {palette.surface};
                border: 1px solid {palette.surface_border};
                border-radius: 6px;
                padding: 5px 12px;
                color: {palette.text};
                text-align: center;
                font-size: 13px;
                min-height: 26px;
            }}
            QWidget#deviceSidebar QPushButton:hover,
            QPushButton#queueTopButton:hover,
            QPushButton#queuePauseButton:hover,
            QPushButton#queueResumeButton:hover,
            QPushButton#queueCancelButton:hover {{
                background: {palette.selected};
                border-color: {palette.primary};
                color: {palette.primary};
            }}
            QWidget#deviceSidebar QPushButton:pressed,
            QPushButton#queueTopButton:pressed,
            QPushButton#queuePauseButton:pressed,
            QPushButton#queueResumeButton:pressed,
            QPushButton#queueCancelButton:pressed {{
                background: {palette.surface_border};
            }}
            QWidget#deviceSidebar QPushButton:disabled,
            QPushButton#queueTopButton:disabled,
            QPushButton#queuePauseButton:disabled,
            QPushButton#queueResumeButton:disabled,
            QPushButton#queueCancelButton:disabled {{
                color: {palette.muted_text};
                border-color: {palette.surface_border};
                background: transparent;
            }}
            QLineEdit, QSpinBox, QComboBox, QListWidget, QTextEdit {{
                background: {palette.surface};
                border: 1px solid {palette.surface_border};
                border-radius: 8px;
                padding: 5px 8px;
                selection-background-color: {palette.selected};
            }}
            QComboBox {{
                background: {palette.surface};
                border: 1px solid {palette.surface_border};
                border-radius: 8px;
                padding: 5px 30px 5px 10px;
                color: {palette.text};
                selection-background-color: {palette.selected};
                min-height: 24px;
            }}
            QComboBox:hover, QComboBox:focus {{
                border-color: {palette.primary};
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 26px;
                border: none;
                background: transparent;
            }}
            QComboBox::down-arrow {{
                image: url({chevron_path});
                width: 12px;
                height: 12px;
            }}
            QComboBox QAbstractItemView {{
                background: {palette.surface};
                border: 1px solid {palette.surface_border};
                border-radius: 8px;
                padding: 4px;
                selection-background-color: {palette.selected};
                selection-color: {palette.primary};
            }}
            QComboBox#themeSelector {{ min-width: 110px; }}
            QComboBox#conflictPolicy {{ min-width: 152px; }}
            QGroupBox {{
                background: {palette.surface};
                border: 1px solid {palette.surface_border};
                border-radius: 10px;
                margin-top: 14px;
                padding: 16px 14px 12px 14px;
                font-size: 13px;
                font-weight: 600;
                color: {palette.text};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 6px;
                background: {palette.window};
                color: {palette.text};
            }}
            QWidget#workspaceToolbar {{
                background: {palette.surface};
                border-bottom: 1px solid {palette.surface_border};
                padding-bottom: 8px;
            }}
            QLabel#workspaceDirection {{ font-size: 15px; font-weight: 700; }}
            QWidget#workspaceActions {{
                background: {palette.surface};
                border-top: 1px solid {palette.surface_border};
                padding-top: 8px;
            }}
            QLabel#filePaneTitle {{ font-size: 13px; font-weight: 700; }}
            QLabel#filePanePath {{ color: {palette.muted_text}; font-size: 12px; }}
            QPushButton#filePaneUpButton, QPushButton#filePaneRefreshButton {{
                background: {palette.surface};
                border: 1px solid {palette.surface_border};
                border-radius: 6px;
                min-width: 24px;
                max-width: 24px;
                min-height: 24px;
                padding: 0;
                text-align: center;
            }}
            QPushButton#filePaneUpButton:hover, QPushButton#filePaneRefreshButton:hover {{
                background: {palette.selected};
                border-color: {palette.primary};
                color: {palette.primary};
            }}
            QPushButton#filePaneNewFolderButton {{
                background: {palette.surface};
                min-height: 24px;
                padding: 2px 8px;
                font-size: 12px;
                border: 1px solid {palette.surface_border};
                border-radius: 6px;
                text-align: center;
            }}
            QPushButton#filePaneNewFolderButton:hover {{
                background: {palette.selected};
                border-color: {palette.primary};
                color: {palette.primary};
            }}
            QTableView#fileTable {{
                background: {palette.surface};
                border: none;
                outline: none;
                selection-background-color: {palette.selected};
                selection-color: {palette.text};
            }}
            QTableView#fileTable::item {{
                border: none;
                padding: 7px 8px;
            }}
            QTableView#fileTable::item:hover {{ background: {palette.selected}; }}
            QTableView#fileTable::item:selected {{
                background: {palette.selected};
                color: {palette.primary};
            }}
            QHeaderView::section {{
                background: {palette.surface};
                color: {palette.muted_text};
                border: none;
                border-bottom: 1px solid {palette.surface_border};
                padding: 7px 8px;
                font-size: 12px;
                font-weight: 600;
            }}
            QSplitter::handle {{ background: transparent; }}
            QSplitter::handle:hover {{ background: {palette.selected}; }}
            QListWidget#queueList, QWidget#deviceSidebar QListWidget {{
                background: transparent;
                border: none;
                padding: 2px 0;
            }}
            QListWidget#queueList::item, QWidget#deviceSidebar QListWidget::item {{
                border: none;
                border-radius: 6px;
                margin: 2px 0;
                padding: 7px 8px;
            }}
            QListWidget#queueList::item:selected, QWidget#deviceSidebar QListWidget::item:selected {{
                background: {palette.selected};
                color: {palette.primary};
            }}
            QScrollBar:vertical {{
                background: transparent;
                border: none;
                margin: 2px;
                width: 10px;
            }}
            QScrollBar::handle:vertical {{
                background: {palette.surface_border};
                border: 2px solid transparent;
                border-radius: 4px;
                background-clip: padding;
                min-height: 36px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {palette.muted_text}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollArea {{ border: none; background: transparent; }}
            QListWidget::item {{
                padding: 10px 8px;
                border-bottom: 1px solid {palette.surface_border};
            }}
            QListWidget::item:selected {{
                background: {palette.selected};
                color: {palette.primary};
            }}
            QProgressBar {{
                border: 1px solid {palette.surface_border};
                border-radius: 6px;
                min-height: 22px;
                text-align: center;
            }}
            QProgressBar::chunk {{
                background: {palette.progress};
                border-radius: 5px;
            }}
            QWidget#taskDetail {{
                background: {palette.surface};
                border-radius: 12px;
            }}
            QLabel#taskStatus {{
                font-size: 26px;
                font-weight: 700;
                padding: 12px 0;
            }}
            QFrame#recoveryCard {{
                background: {palette.recovery_bg};
                border: 1px solid {palette.recovery_border};
                border-radius: 12px;
            }}
            QLabel#recoveryTitle {{
                color: {palette.recovery_text};
                font-weight: 700;
                background: transparent;
            }}
            QFrame#recoveryCard QLabel {{
                background: transparent;
                color: {palette.recovery_text};
            }}
            QLabel[taskState="running"], QLabel#taskStatus[taskState="running"] {{ color: {palette.status_running}; }}
            QLabel[taskState="recovered"], QLabel[taskState="completed"], QLabel#taskStatus[taskState="recovered"], QLabel#taskStatus[taskState="completed"] {{ color: {palette.status_success}; }}
            QLabel[taskState="warning"], QLabel#taskStatus[taskState="warning"] {{ color: {palette.status_warning}; }}
            QLabel[taskState="failure"], QLabel[taskState="failed"], QLabel#taskStatus[taskState="failure"], QLabel#taskStatus[taskState="failed"] {{ color: {palette.status_danger}; }}
        """

    def _saved_theme(self) -> ThemeName:
        value = str(self._settings.value("appearance/theme", ThemeName.SYSTEM.value))
        try:
            return ThemeName(value)
        except ValueError:
            return ThemeName.SYSTEM


__all__ = ["ThemeController", "ThemeName", "ThemePalette"]
