"""The app's colours and widget stylesheets, in one place.

Redesign prep: every ``setStyleSheet`` string and UI colour literal
lives here so a restyle edits one file. Values were lifted verbatim
from the call sites — several greys/darks are near-duplicates kept
distinct on purpose (byte-identical migration); merge them when the
redesign picks the final palette.

Layout: a small palette of cross-cutting tokens first (colours that
code needs directly, or that many styles share), then style strings
grouped by component. Dynamic styles are functions.
"""

# ----------------------------------------------------- canonical palette
# The redesign's target tokens (2026-09-04). New/updated styles use these;
# the legacy near-duplicates below migrate onto them step by step.

BG_DEEP = "#080b0f"     # wells: input fields, troughs, hover previews
BG = "#0d1116"          # app base / panel surfaces
BG_RAISED = "#151b22"   # cards, headers, buttons
BG_HOVER = "#1e2630"    # hover states, alternate rows

BORDER = "#2e3b47"          # the border colour
BORDER_LIGHT = "#3d4c5a"    # emphasised edges (focused inputs, handles)

TEXT_BRIGHT = "#ecf0f4"     # titles, selected-state text
TEXT_DISABLED = "#6b7681"   # disabled controls

ACCENT = "#5e9bff"          # the app accent (links, focus, progress)
ACCENT_DIM = "#24435f"      # selected/checked fills behind light text
ACCENT_BORDER = "#3d6288"   # border partnering ACCENT_DIM fills

FONT_SCALE = 1.3            # applied to the app default font at startup

# User zoom on top of FONT_SCALE (Chris, 2026-09-05): Ctrl+= / Ctrl+- /
# Ctrl+0 from the top-right menu, live and remembered per user. Scales
# the application font, which drives text and button sizes; scene-based
# views ask zoom_factor() for their row geometry.
ZOOM_MIN = 0.6
ZOOM_MAX = 2.0
ZOOM_STEP = 0.1
_ZOOM_KEY = "ui_zoom"
_base_point_size: float | None = None
_zoom: float = 1.0


def _load_zoom() -> float:
    try:
        from logfather.data.ui_state_store import load_ui_state

        raw = float(load_ui_state().get(_ZOOM_KEY) or 1.0)
    except Exception:
        raw = 1.0
    return max(ZOOM_MIN, min(ZOOM_MAX, raw))


def zoom_factor() -> float:
    return _zoom


def set_zoom(app, value: float) -> float:
    """Apply a zoom level to the running app and remember it."""
    global _zoom, _base_point_size
    _zoom = max(ZOOM_MIN, min(ZOOM_MAX, round(float(value), 2)))
    if _base_point_size is None:
        _base_point_size = app.font().pointSizeF() / FONT_SCALE
    font = app.font()
    font.setPointSizeF(_base_point_size * FONT_SCALE * _zoom)
    app.setFont(font)
    try:
        from logfather.data.ui_state_store import update_ui_state

        update_ui_state({_ZOOM_KEY: _zoom})
    except Exception:
        pass
    return _zoom

# ---------------------------------------------------------------- palette

TEXT = "#d7dde2"          # primary light text
TEXT_MUTED = "#9aa0a6"    # captions, version labels
TEXT_DIM = "#888888"      # hints, disabled-ish labels
TEXT_FAINT = "#7f8c8d"    # smallest annotations (card keys, dialog hints)

SUCCESS = "#2e7d32"        # sync-done button
SUCCESS_BRIGHT = "#2ecc71" # lock-on glyph, buffer "add" events
DANGER = "#ff4d4f"         # lock-off glyph
WARNING_BORDER = "#cc8800" # invalid input field
# Status colours that were repeated as literals across the ui (review
# 2026-09-14): amber for "check this" (manual mode, a date mismatch, a
# cancelled step), soft red for "wrong / stopped", and the two OCR boxes.
WARNING = "#f0ad4e"        # amber: manual mode, date differs, cancelled
DANGER_SOFT = "#ff7a70"    # soft red: does not match, stopped, no date sync
OCR_TIME = "#00ff5a"       # the green Time box in Sync CCTV Time
OCR_DATE = "#c77dff"       # the purple Date box in Sync CCTV Time

LCD_GREEN = "#00ff66"

# Fleetwide legend / graph accents (also used inline in rich text)
LEGEND_OPERATION = "#e74c3c"
LEGEND_STARTUP = "#f39c12"

# Stop-report category accents (blended with the widget palette)
STOP_ACCENT_ESTOP = "#c85c5c"
STOP_ACCENT_CAUTION = "#c98732"
STOP_ACCENT_OPERATOR = "#4b7fc7"

# Target-buffer card palette
CARD_BG = BG_RAISED
CARD_BG_ALT = BG_HOVER     # odd product_id — slightly lighter
CARD_BORDER = BORDER
HEADER_BG = BG
CARD_INVALID_BG = "#2a1a1a"
CARD_INVALID_BORDER = "#5c2020"
GAP_CLOSE_BORDER = "#f1c40f"
GAP_CLOSE_BG_ODD = "#3a3314"
GAP_CLOSE_BG_EVEN = "#332d12"
GAP_WIDE_BORDER = "#4aa3ff"
GAP_WIDE_BG_ODD = "#152d45"
GAP_WIDE_BG_EVEN = "#13283d"

# ------------------------------------------------------------ shared roles

MUTED_LABEL = f"color: {TEXT_MUTED};"
DIM_LABEL = f"color: {TEXT_DIM};"
HINT_LABEL = f"color: {TEXT_FAINT}; font-size: 13px;"
TITLE_LABEL = "font-size: 28px; font-weight: bold;"
ERROR_LABEL = "color: #ff8a80;"
PRIMARY_ACTION_BUTTON = "font-weight: bold; padding: 6px;"
MONO_VALUE_LABEL = f"font-family: monospace; color: {TEXT};"

LCD_DISPLAY = f"QLCDNumber {{ background-color: #000000; color: {LCD_GREEN}; }}"

SLIDER_CAPTION = f"color: {TEXT_MUTED}; font-size: 13px;"
SLIDER_VALUE = f"color: {TEXT}; font-family: Consolas, monospace; font-size: 13px;"

SYNC_DONE_BUTTON = f"background-color: {SUCCESS}; color: white;"
LOCK_ON_LABEL = f"color: {SUCCESS_BRIGHT};"
LOCK_OFF_LABEL = f"color: {DANGER};"
INPUT_WARNING_BORDER = f"border: 1px solid {WARNING_BORDER};"


def solid_button(color_name: str) -> str:
    """Colour-swatch button (annotation colour picker)."""
    return f"background-color: {color_name};"


# ----------------------------------------------------------------- viewer

LOG_LIST = """
    QListView::item:selected {
        background-color: #cc2222;
        color: white;
    }
    QListView::item:selected:!active {
        background-color: #882222;
        color: white;
    }
"""

PIN_BUTTON = (
    "QPushButton { border: none; background: transparent; font-size: 17px; }"
    "QPushButton:checked { background: rgba(255,255,255,30); border-radius: 3px; }"
)

# ------------------------------------------------------------ main window

TOP_BAR_LABEL = f"color: {TEXT}; padding-left: 8px;"

# Segmented mode switcher: three joined buttons; the global QToolButton
# style supplies fills/checked accent, these only join the edges.
SEGMENT_LEFT = (
    "QToolButton { border-top-right-radius: 0px; border-bottom-right-radius: 0px; }"
)
SEGMENT_MID = "QToolButton { border-radius: 0px; border-left: none; }"
SEGMENT_RIGHT = (
    "QToolButton { border-top-left-radius: 0px; border-bottom-left-radius: 0px;"
    " border-left: none; }"
)

# "⋯" overflow button: keep it quiet, no menu arrow.
OVERFLOW_BUTTON = (
    "QToolButton { font-weight: bold; padding: 4px 13px; }"
    "QToolButton::menu-indicator { image: none; }"
)
# Gear button top-right on every window (Chris, 2026-09-07).
GEAR_BUTTON = (
    "QToolButton { padding: 3px 7px; }"
    "QToolButton::menu-indicator { image: none; }"
)

# "v0.207 available · Restart" pill in the top bar (Chris, 2026-09-07).
UPDATE_PILL = (
    f"QPushButton {{ background-color: {WARNING_BORDER}; color: #1a1200; font-weight: bold;"
    " border: none; border-radius: 12px; padding: 4px 14px; }"
    "QPushButton:hover { background-color: #e39a12; }"
)

# Zoom row in the overflow menu: (-) Zoom (+) as circled buttons.
ZOOM_CIRCLE_SIZE = 22
ZOOM_CIRCLE_BUTTON = (
    f"QToolButton {{ border: 1px solid {BORDER_LIGHT}; border-radius: {ZOOM_CIRCLE_SIZE // 2}px;"
    f" background: {BG}; color: {TEXT}; font-weight: bold; padding: 0; }}"
    f"QToolButton:hover {{ background: {BG_HOVER}; border-color: {ACCENT}; }}"
    f"QToolButton:pressed {{ background: {BORDER}; }}"
    f"QToolButton:disabled {{ color: {BORDER_LIGHT}; border-color: {BORDER}; }}"
)

SHUTDOWN_POPUP = (
    f"QWidget {{ background-color: {BG}; color: {TEXT};"
    f" border: 1px solid {BORDER_LIGHT}; font-size: 16px; }}"
    f"QProgressBar {{ border: 1px solid {BORDER}; background: {BG_DEEP};"
    " height: 12px; text-align: center; }"
    f"QProgressBar::chunk {{ background-color: {ACCENT}; }}"
)
POPUP_TITLE = "border: none; font-weight: bold;"
POPUP_STEP = f"border: none; color: {TEXT_MUTED};"

# ------------------------------------------------------------- date picker

SIM_BUTTON = (
    "QPushButton { padding: 2px 6px; } "
    "QPushButton:checked { "
    f"  background-color: {ACCENT_DIM}; "
    f"  border: 1px solid {ACCENT_BORDER}; "
    f"  color: {TEXT_BRIGHT}; "
    "}"
)

CUSTOMER_HEADER_BUTTON = (
    "QPushButton { "
    f"text-align: left; padding: 6px 8px; color: {TEXT}; font-weight: bold; "
    f"background: {BG_RAISED}; border: 1px solid {BORDER}; }} "
    f"QPushButton:hover {{ background: {BG_HOVER}; color: {TEXT_BRIGHT}; }}"
)

SYSTEM_BUTTON = (
    "QPushButton { "
    "  padding: 2px 6px; text-align: left; "
    f"  background-color: {BG_RAISED}; color: {TEXT_BRIGHT}; "
    f"  border: 1px solid {BORDER}; border-right: 0px; "
    "  border-top-left-radius: 6px; border-bottom-left-radius: 6px; "
    "  border-top-right-radius: 0px; border-bottom-right-radius: 0px; "
    "} "
    f"QPushButton:hover {{ background-color: {BG_HOVER}; }} "
    f"QPushButton:checked {{ background-color: {ACCENT_DIM}; border: 1px solid {ACCENT}; border-right: 0px; color: {TEXT_BRIGHT}; }}"
)

TODAY_BUTTON = (
    "QPushButton { "
    "  padding: 0px; font-weight: bold; "
    f"  background-color: {BG_HOVER}; color: {TEXT_BRIGHT}; "
    f"  border: 1px solid {BORDER}; border-left: 1px solid {BORDER_LIGHT}; "
    "  border-top-left-radius: 0px; border-bottom-left-radius: 0px; "
    "  border-top-right-radius: 6px; border-bottom-right-radius: 6px; "
    "} "
    f"QPushButton:hover {{ background-color: {BORDER}; }} "
    f"QPushButton:pressed {{ background-color: {BORDER_LIGHT}; }}"
)

# --------------------------------------------------------------- overview

OVERVIEW_STATUS = f"color: {TEXT_MUTED};"
PANEL_SURFACE = f"background: {BG}; border: 1px solid {BORDER};"
PANEL_BG = f"background: {BG};"
HOVER_PREVIEW = f"background: {BG_DEEP}; border: 1px solid {BORDER}; padding: 4px;"
LOADING_BADGE = (
    "background: rgba(9, 13, 17, 180);"
    f"color: {TEXT_BRIGHT};"
    "padding: 10px 14px;"
    f"border: 1px solid {BORDER};"
    "border-radius: 6px;"
    "font-size: 18px;"
    "font-weight: 600;"
)

# -------------------------------------------------------------- fleetwide

FLEETWIDE_CARD = f"QFrame#fleetwideSystemCard {{ background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 8px; }}"
FLEETWIDE_MUTED = f"color: {TEXT_MUTED};"
FLEETWIDE_NOTE = f"color: {TEXT_MUTED};"
FLEETWIDE_EMPTY = f"color: {TEXT_MUTED}; padding: 30px;"

# ------------------------------------------------------------ stop report

MEDIA_HOLDER = "background: #000000; border-radius: 8px;"
THUMB_BUTTON = (
    "QPushButton { border: none; border-radius: 8px; background: transparent; }"
    f"QPushButton:disabled {{ color: {TEXT_MUTED}; background-color: {BG_RAISED}; }}"
)


def report_row_style(bg_name: str, border_name: str) -> str:
    """Stop-report entry row, colours blended per category at runtime."""
    return f"background-color: {bg_name}; border: 1px solid {border_name}; border-radius: 6px;"


# ---------------------------------------------------------- target buffer

VALUE_LABEL = f"color: {TEXT_BRIGHT}; font-size: 13px;"
CARD_TITLE = f"color: {TEXT_BRIGHT}; font-weight: bold; font-size: 14px;"
CARD_TIME = f"color: {TEXT_FAINT}; font-size: 13px; margin-left: 4px;"
CHEVRON = f"color: {BORDER_LIGHT}; font-size: 10px;"
SEPARATOR = f"color: {CARD_BORDER};"
EMPTY_NOTE_INLINE = "color:#566573;font-size:13px;font-style:italic;"
EMPTY_STATE = "color: #566573; font-size: 14px; font-style: italic; padding: 16px;"
NO_DATA_STATE = "color: #3d4f5c; font-size: 14px; font-style: italic; padding: 16px;"
BUFFER_HEADER = (
    f"background: {HEADER_BG}; color: {TEXT}; font-weight: bold; "
    "padding: 6px 8px; font-size: 16px;"
)
BUFFER_SCROLL = f"QScrollArea {{ background: {BG}; border: none; }}"
BUFFER_BG = f"background: {BG};"


def target_card_style(bg: str, border: str) -> str:
    """Target card frame; bg/border picked from validity + gap status."""
    return (
        f"QFrame {{ background: {bg}; border: 1px solid {border}; "
        "border-radius: 4px; margin: 2px 4px; }"
    )


# ------------------------------------------------------------ calibration

# Tracking-line overlay on the CCTV frame: vivid magenta — practically
# never present in factory footage, unlike the old orange.
CAL_TRACK_LINE = "#ff00d9"

# Results block: monospace so the x/y columns line up like a table.
CAL_RESULTS_TEXT = f"color: {TEXT_FAINT}; font-size: 13px; font-family: Consolas, monospace;"

# ---------------------------------------------------------- activity bar

ACTIVITY_BAR = f"background: {BG}; border-top: 1px solid {BORDER};"
ACTIVITY_BAR_TEXT = f"color: {TEXT_MUTED}; font-size: 14px; border: none; background: transparent;"
ACTIVITY_PROGRESS = (
    f"QProgressBar {{ border: 1px solid {BORDER}; background: {BG_DEEP}; }}"
    f"QProgressBar::chunk {{ background-color: {SUCCESS_BRIGHT}; }}"
)

# ------------------------------------------------------- dialogs / pages

ABOUT_PAGE = (
    f"QDialog {{ background: {BG}; }}"
    f"QLabel {{ color: {TEXT}; }}"
    f"QTabWidget::pane {{ border: 1px solid {BORDER}; background: {BG}; }}"
    f"QTabBar::tab {{ background: {BG_RAISED}; color: {TEXT_MUTED}; padding: 6px 16px; }}"
    f"QTabBar::tab:selected {{ background: {ACCENT_DIM}; color: {TEXT_BRIGHT}; }}"
    f"QPushButton {{ background: {ACCENT_DIM}; color: {TEXT_BRIGHT}; border: 1px solid {ACCENT_BORDER};"
    " border-radius: 4px; padding: 5px 18px; }"
    f"QScrollArea {{ border: none; background: {BG}; }}"
    f"QTextBrowser {{ background: {BG}; border: none; }}"
)
ABOUT_MUTED = f"color: {TEXT_MUTED};"

LOGO_PREVIEW = f"border: 1px solid {BORDER}; background: {BG}; color: {TEXT_MUTED};"

# ------------------------------------------------------- application base
# Global dark base: Fusion style + palette + this stylesheet, applied once
# at startup. Before this the app had NO global styling - every unstyled
# widget (combos, scrollbars, tabs, checkboxes, menus, the calendar) drew
# in the OS light theme, patched over by per-widget dark styles. Component
# styles above still win where they set the same properties.

APP_STYLESHEET = f"""
QToolTip {{
    background-color: {BG_RAISED}; color: {TEXT};
    border: 1px solid {BORDER}; padding: 3px 6px;
}}

QPushButton, QToolButton {{
    background-color: {BG_RAISED}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 4px;
    padding: 6px 13px;
}}
QToolButton {{ padding: 4px 10px; }}
QPushButton:hover, QToolButton:hover {{ background-color: {BG_HOVER}; }}
QPushButton:pressed, QToolButton:pressed {{ background-color: {BG_DEEP}; }}
QPushButton:checked, QToolButton:checked {{
    background-color: {ACCENT_DIM}; color: {TEXT_BRIGHT};
    border-color: {ACCENT_BORDER};
}}
QPushButton:disabled, QToolButton:disabled {{
    color: {TEXT_DISABLED}; background-color: {BG}; border-color: {BORDER};
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QTextEdit, QPlainTextEdit {{
    background-color: {BG_DEEP}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 4px;
    padding: 2px 6px;
    selection-background-color: {ACCENT_DIM}; selection-color: {TEXT_BRIGHT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {BORDER_LIGHT};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {BG_RAISED}; color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {BG_HOVER}; selection-color: {TEXT_BRIGHT};
}}

QTabWidget::pane {{ border: 1px solid {BORDER}; }}
QTabBar::tab {{
    background-color: {BG}; color: {TEXT_MUTED};
    border: 1px solid {BORDER}; border-bottom: none;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
    padding: 7px 15px; margin-right: 1px;
}}
QTabBar::tab:hover {{ background-color: {BG_HOVER}; color: {TEXT}; }}
QTabBar::tab:selected {{ background-color: {BG_RAISED}; color: {TEXT_BRIGHT}; }}

QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BG_HOVER}; border-radius: 5px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {BORDER_LIGHT}; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BG_HOVER}; border-radius: 5px; min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {BORDER_LIGHT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{
    background-color: {BG_RAISED}; color: {TEXT};
    border: 1px solid {BORDER};
}}
QMenu::item {{ padding: 4px 20px; }}
QMenu::item:selected {{ background-color: {BG_HOVER}; }}

QGroupBox {{
    border: 1px solid {BORDER}; border-radius: 4px;
    margin-top: 8px; padding-top: 4px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 8px; padding: 0 3px;
    color: {TEXT_MUTED};
}}

QHeaderView::section {{
    background-color: {BG_RAISED}; color: {TEXT};
    border: none; border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER}; padding: 3px 6px;
}}

QCalendarWidget QWidget#qt_calendar_navigationbar {{ background-color: {BG_RAISED}; }}
"""


def apply_app_theme(app) -> None:
    """Fusion style + dark palette + base stylesheet, once per process."""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QStyleFactory

    app.setStyle(QStyleFactory.create("Fusion"))

    # ~30% larger type app-wide (Chris, 2026-09-05). Scaling the default
    # font covers every widget and graphics-scene text that doesn't set
    # its own size; the explicit px sizes above are pre-scaled to match.
    global _base_point_size, _zoom
    _base_point_size = app.font().pointSizeF()
    _zoom = _load_zoom()
    font = app.font()
    font.setPointSizeF(_base_point_size * FONT_SCALE * _zoom)
    app.setFont(font)

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Base, QColor(BG_DEEP))
    pal.setColor(QPalette.AlternateBase, QColor(BG_RAISED))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(BG_RAISED))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.BrightText, QColor(TEXT_BRIGHT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT_DIM))
    pal.setColor(QPalette.HighlightedText, QColor(TEXT_BRIGHT))
    pal.setColor(QPalette.Link, QColor(ACCENT))
    pal.setColor(QPalette.ToolTipBase, QColor(BG_RAISED))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.PlaceholderText, QColor(TEXT_FAINT))
    disabled = QColor(TEXT_DISABLED)
    pal.setColor(QPalette.Disabled, QPalette.Text, disabled)
    pal.setColor(QPalette.Disabled, QPalette.WindowText, disabled)
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, disabled)
    pal.setColor(QPalette.Disabled, QPalette.Highlight, QColor(BORDER))
    app.setPalette(pal)

    app.setStyleSheet(APP_STYLESHEET)
