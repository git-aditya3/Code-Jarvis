"""Dark "arc reactor" visual theme for the JARVIS interface."""

from __future__ import annotations

ACCENTS: dict[str, dict[str, str]] = {
    "cyan": {
        "accent": "#22d3ee", "accent_dim": "#0e7490", "accent_soft": "#67e8f9",
        "glow": "rgba(34, 211, 238, 0.35)",
    },
    "amber": {
        "accent": "#fbbf24", "accent_dim": "#b45309", "accent_soft": "#fcd34d",
        "glow": "rgba(251, 191, 36, 0.35)",
    },
    "violet": {
        "accent": "#a78bfa", "accent_dim": "#6d28d9", "accent_soft": "#c4b5fd",
        "glow": "rgba(167, 139, 250, 0.35)",
    },
    "emerald": {
        "accent": "#34d399", "accent_dim": "#047857", "accent_soft": "#6ee7b7",
        "glow": "rgba(52, 211, 153, 0.35)",
    },
}

BASE = {
    "bg": "#05070d",
    "panel": "#0a1020",
    "panel_alt": "#0e1729",
    "border": "#1b2740",
    "border_soft": "#141d31",
    "text": "#dce7ff",
    "muted": "#7d90b3",
    "dim": "#4c5b78",
    "ok": "#34d399",
    "warn": "#fbbf24",
    "error": "#f87171",
    "user": "#93c5fd",
}

FONT_STACK = '"Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", "DejaVu Sans", sans-serif'
MONO_STACK = '"Cascadia Code", "JetBrains Mono", "Consolas", "DejaVu Sans Mono", monospace'

STATE_COLORS = {
    "idle": "accent",
    "listening": "#34d399",
    "thinking": "#fbbf24",
    "speaking": "#22d3ee",
    "error": "#f87171",
}


def accent_colors(name: str) -> dict[str, str]:
    return ACCENTS.get(name, ACCENTS["cyan"])


def stylesheet(accent: str = "cyan") -> str:
    """Return the app-wide QSS for the requested accent colour."""
    a = accent_colors(accent)
    return f"""
    QWidget {{
        color: {BASE['text']};
        font-family: {FONT_STACK};
        font-size: 13px;
    }}
    #Window {{
        background: {BASE['bg']};
        border: 1px solid {BASE['border']};
        border-radius: 14px;
    }}
    #Window QDialog, #ConfirmDialog {{
        background: {BASE['bg']};
        border: 1px solid {BASE['border']};
        border-radius: 12px;
    }}
    #ConfirmDialog QLabel, #ConfirmDialog QCheckBox {{ color: {BASE['text']}; }}
    #TitleBar {{ background: transparent; }}
    #TitleText {{
        font-size: 14px;
        font-weight: 600;
        letter-spacing: 3px;
        color: {a['accent']};
    }}
    #TitleSub {{ color: {BASE['muted']}; font-size: 11px; letter-spacing: 1px; }}
    #Panel {{
        background: {BASE['panel']};
        border: 1px solid {BASE['border_soft']};
        border-radius: 12px;
    }}
    #PanelAlt {{
        background: {BASE['panel_alt']};
        border: 1px solid {BASE['border_soft']};
        border-radius: 10px;
    }}
    QLabel#Muted, QLabel#Hint {{ color: {BASE['muted']}; }}
    QLabel#SectionTitle {{
        color: {a['accent_soft']};
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 2px;
    }}
    QLabel#Value {{ font-size: 15px; font-weight: 600; }}
    QPushButton {{
        background: {BASE['panel_alt']};
        border: 1px solid {BASE['border']};
        border-radius: 8px;
        padding: 7px 12px;
        color: {BASE['text']};
    }}
    QPushButton:hover {{ border-color: {a['accent_dim']}; background: #12203a; }}
    QPushButton:pressed {{ background: #16263f; }}
    QPushButton:disabled {{ color: {BASE['dim']}; border-color: {BASE['border_soft']}; }}
    QPushButton#Primary {{
        background: {a['accent_dim']};
        border: 1px solid {a['accent']};
        color: #f8fbff;
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {a['accent']}; color: #04121b; }}
    QPushButton#Ghost {{ background: transparent; border: none; color: {BASE['muted']}; padding: 6px 9px; }}
    QPushButton#Ghost:hover {{ color: {a['accent']}; }}
    QPushButton#Chip {{
        background: {BASE['panel_alt']};
        border: 1px solid {BASE['border_soft']};
        border-radius: 13px;
        padding: 5px 11px;
        color: {BASE['muted']};
        font-size: 12px;
    }}
    QPushButton#Chip:hover {{ color: {a['accent']}; border-color: {a['accent_dim']}; }}
    QPushButton#Mic {{
        background: {BASE['panel_alt']};
        border: 1px solid {a['accent_dim']};
        border-radius: 24px;
        font-size: 18px;
        padding: 0px;
    }}
    QPushButton#Mic:hover {{ border-color: {a['accent']}; }}
    QPushButton#Mic[active="true"] {{
        background: {a['accent_dim']};
        border: 1px solid {a['accent']};
    }}
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background: {BASE['panel_alt']};
        border: 1px solid {BASE['border']};
        border-radius: 8px;
        padding: 7px 9px;
        selection-background-color: {a['accent_dim']};
    }}
    QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {a['accent_dim']}; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {BASE['panel']};
        border: 1px solid {BASE['border']};
        selection-background-color: {a['accent_dim']};
    }}
    QTextBrowser {{
        background: {BASE['panel']};
        border: 1px solid {BASE['border_soft']};
        border-radius: 10px;
        padding: 6px;
    }}
    QTabWidget::pane {{ border: none; background: transparent; }}
    QTabBar::tab {{
        background: transparent;
        color: {BASE['muted']};
        padding: 8px 14px;
        margin-right: 4px;
        border-bottom: 2px solid transparent;
        font-size: 12px;
        letter-spacing: 1px;
    }}
    QTabBar::tab:hover {{ color: {BASE['text']}; }}
    QTabBar::tab:selected {{ color: {a['accent']}; border-bottom: 2px solid {a['accent']}; }}
    QListWidget, QTableWidget {{
        background: {BASE['panel']};
        border: 1px solid {BASE['border_soft']};
        border-radius: 10px;
        padding: 4px;
    }}
    QListWidget::item {{ padding: 7px 9px; border-radius: 6px; }}
    QListWidget::item:selected {{ background: {a['accent_dim']}; color: #f8fbff; }}
    QListWidget::item:hover {{ background: #12203a; }}
    QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {BASE['border']}; border-radius: 4px; min-height: 26px; }}
    QScrollBar::handle:vertical:hover {{ background: {a['accent_dim']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {BASE['border']}; border-radius: 4px; min-width: 26px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QCheckBox {{ color: {BASE['text']}; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px; border-radius: 4px;
        border: 1px solid {BASE['border']}; background: {BASE['panel_alt']};
    }}
    QCheckBox::indicator:checked {{ background: {a['accent']}; border-color: {a['accent']}; }}
    QSlider::groove:horizontal {{ height: 4px; background: {BASE['border']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: {a['accent']}; width: 13px; height: 13px; margin: -5px 0; border-radius: 6px;
    }}
    QProgressBar {{
        background: {BASE['panel_alt']};
        border: 1px solid {BASE['border_soft']};
        border-radius: 6px;
        text-align: center;
        color: {BASE['muted']};
        font-size: 11px;
    }}
    QProgressBar::chunk {{ background: {a['accent_dim']}; border-radius: 5px; }}
    QToolTip {{
        background: {BASE['panel_alt']};
        color: {BASE['text']};
        border: 1px solid {BASE['border']};
        padding: 5px;
    }}
    QMenu {{
        background: {BASE['panel']};
        border: 1px solid {BASE['border']};
        padding: 4px;
    }}
    QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {a['accent_dim']}; }}
    QSplitter::handle {{ background: {BASE['border_soft']}; width: 1px; }}

    /* ── modern panels: cards, tiles, pills, palette ─────────────────── */
    #Card {{
        background: {BASE['panel']};
        border: 1px solid {BASE['border_soft']};
        border-radius: 12px;
    }}
    #Card QLabel#CardTitle, QLabel#CardTitle {{
        color: {a['accent_soft']};
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 2px;
    }}
    #Card QTextBrowser#CardBody {{
        background: {BASE['panel_alt']};
        border: 1px solid {BASE['border_soft']};
        border-radius: 9px;
    }}
    QLabel#Body {{ color: {BASE['text']}; }}
    #Tile {{
        background: {BASE['panel_alt']};
        border: 1px solid {BASE['border']};
        border-radius: 12px;
        min-width: 130px;
    }}
    #Tile[tone="accent"] {{ border-color: {a['accent_dim']}; }}
    #Tile[tone="ok"] {{ border-color: {BASE['ok']}; }}
    #Tile[tone="warn"] {{ border-color: {BASE['warn']}; }}
    #Tile[tone="info"] {{ border-color: {BASE['user']}; }}
    QLabel#TileValue {{ font-size: 22px; font-weight: 700; color: {BASE['text']}; }}
    QLabel#TileValue[tone="accent"] {{ color: {a['accent']}; }}
    QLabel#TileValue[tone="ok"] {{ color: {BASE['ok']}; }}
    QLabel#TileValue[tone="warn"] {{ color: {BASE['warn']}; }}
    QLabel#TileValue[tone="info"] {{ color: {BASE['user']}; }}
    QLabel#TileLabel {{ color: {BASE['muted']}; font-size: 10px; letter-spacing: 2px; }}
    QLabel#Pill {{
        border-radius: 11px;
        padding: 4px 10px;
        font-size: 12px;
        background: {BASE['panel_alt']};
        border: 1px solid {BASE['border']};
        color: {BASE['muted']};
    }}
    QLabel#Pill[tone="ok"] {{ color: {BASE['ok']}; border-color: {BASE['ok']}; }}
    QLabel#Pill[tone="info"] {{ color: {BASE['user']}; border-color: {BASE['border']}; }}
    QLabel#Pill[tone="muted"] {{ color: {BASE['dim']}; }}
    QLabel#HourCell {{ font-size: 10px; color: {BASE['dim']}; }}
    #Palette {{ background: {BASE['bg']}; border: 1px solid {a['accent_dim']}; border-radius: 12px; }}
    #Palette QListWidget {{ background: {BASE['panel']}; border: 1px solid {BASE['border_soft']}; }}
    #Palette QListWidget::item {{ padding: 8px 10px; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    """
