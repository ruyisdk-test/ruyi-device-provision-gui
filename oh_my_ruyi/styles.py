"""Design System tokens and QSS stylesheet generation for Oh My Ruyi."""

from __future__ import annotations

from PySide6.QtGui import QPalette


def resolve_theme_colors(palette: QPalette) -> dict[str, str]:
    """Extract named theme color tokens from a Qt QPalette."""

    def color(role: QPalette.ColorRole) -> str:
        return palette.color(role).name()

    is_dark = palette.color(QPalette.ColorRole.Window).lightness() < 128
    return {
        "window": color(QPalette.ColorRole.Window),
        "window_text": color(QPalette.ColorRole.WindowText),
        "base": color(QPalette.ColorRole.Base),
        "text": color(QPalette.ColorRole.Text),
        "button": color(QPalette.ColorRole.Button),
        "button_text": color(QPalette.ColorRole.ButtonText),
        "border": color(QPalette.ColorRole.Mid),
        "highlight": color(QPalette.ColorRole.Highlight),
        "highlighted_text": color(QPalette.ColorRole.HighlightedText),
        "disabled_button": palette.color(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.Button,
        ).name(),
        "disabled_text": palette.color(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.Text,
        ).name(),
        "success": "#7ee787" if is_dark else "#1a7f37",
        "warning": "#f2cc60" if is_dark else "#9a6700",
        "error": "#ff7b72" if is_dark else "#cf222e",
    }


def build_stylesheet(palette: QPalette) -> str:
    """Generate application QSS stylesheet using active palette tokens."""
    colors = resolve_theme_colors(palette)
    return f"""
    QMainWindow {{ background: {colors["window"]}; color: {colors["window_text"]}; }}
    QWidget {{ color: {colors["window_text"]}; }}
    QListWidget#stepList {{
        background: {colors["base"]};
        color: {colors["text"]};
        border: 1px solid {colors["border"]};
        border-radius: 6px;
        padding: 4px;
    }}
    QListWidget#stepList::item {{ min-height: 34px; padding: 3px 7px; }}
    QListWidget#stepList::item:selected {{
        background: {colors["highlight"]};
        color: {colors["highlighted_text"]};
    }}
    QListWidget#stepList::item:disabled {{ color: {colors["disabled_text"]}; }}
    QGroupBox {{
        background: {colors["base"]};
        color: {colors["text"]};
        border: 1px solid {colors["border"]};
        border-radius: 6px;
        margin-top: 9px;
        padding: 8px;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
    QGroupBox QLabel {{ color: {colors["text"]}; }}
    QLabel#pageTitle {{ font-size: 17px; color: {colors["window_text"]}; }}
    QLabel#postInstallMessage {{
        padding: 8px;
        background: {colors["base"]};
        color: {colors["text"]};
        border: 1px solid {colors["border"]};
    }}
    QLabel[statusKind="success"] {{ color: {colors["success"]}; font-weight: 600; }}
    QLabel[statusKind="warning"] {{ color: {colors["warning"]}; font-weight: 600; }}
    QLabel[statusKind="error"] {{ color: {colors["error"]}; font-weight: 600; }}
    QPushButton {{
        min-height: 30px;
        padding: 2px 10px;
        background: {colors["button"]};
        color: {colors["button_text"]};
        border: 1px solid {colors["border"]};
        border-radius: 4px;
    }}
    QPushButton#primaryButton {{
        background: {colors["highlight"]};
        color: {colors["highlighted_text"]};
        border-color: {colors["highlight"]};
    }}
    QPushButton:disabled {{
        background: {colors["disabled_button"]};
        color: {colors["disabled_text"]};
    }}
    QPushButton#primaryButton:disabled {{
        background: {colors["disabled_button"]};
        color: {colors["disabled_text"]};
        border-color: {colors["border"]};
    }}
    QLineEdit, QComboBox, QListWidget, QTableWidget, QPlainTextEdit, QTextEdit {{
        background: {colors["base"]};
        color: {colors["text"]};
        selection-background-color: {colors["highlight"]};
        selection-color: {colors["highlighted_text"]};
        border: 1px solid {colors["border"]};
    }}
    QLineEdit:disabled, QComboBox:disabled, QListWidget:disabled,
    QTableWidget:disabled,
    QPlainTextEdit:disabled, QTextEdit:disabled, QCheckBox:disabled {{
        background: {colors["disabled_button"]};
        color: {colors["disabled_text"]};
    }}
    QLabel#versionStatus {{
        padding: 0;
        background: transparent;
        color: {colors["window_text"]};
        border: none;
        font-weight: normal;
    }}
    QLabel#versionStatus[statusKind="error"] {{ color: {colors["error"]}; font-weight: normal; }}
    """
