"""
Theme module — Modern dark theme inspired by tennisratio.com.
Colors, fonts, QSS stylesheet, DWM dark title bar, and font loading.
"""

import ctypes
import os
import sys

from PySide6.QtGui import QFontDatabase

# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------
_fonts_loaded = False


def load_fonts():
    """Load bundled Inter font. Call once at application startup."""
    global _fonts_loaded
    if _fonts_loaded:
        return
    fonts_dir = os.path.join(os.path.dirname(__file__), "fonts")
    for name in os.listdir(fonts_dir):
        if name.lower().endswith((".ttf", ".otf")):
            path = os.path.join(fonts_dir, name)
            QFontDatabase.addApplicationFont(path)
    _fonts_loaded = True


# ---------------------------------------------------------------------------
# Color palette — Modern dark (tennisratio-inspired)
# ---------------------------------------------------------------------------
COLORS = {
    # Backgrounds
    "bg_primary":   "#0f1923",
    "bg_secondary": "#0a1219",
    "bg_card":      "#162a3a",
    "bg_hover":     "#1e3a4f",
    "bg_input":     "#12232e",
    # Borders
    "border":       "#1e3a4f",
    "border_light": "#2a4a5f",
    # Text
    "text":         "#e8edf2",
    "text_dim":     "#8899aa",
    "text_muted":   "#556677",
    # Accents
    "accent":       "#00b8d4",
    "accent_hover": "#00e5ff",
    "accent_dim":   "rgba(0, 184, 212, 0.15)",
    # Semantic
    "green":        "#4caf50",
    "green_bright": "#66bb6a",
    "red":          "#ef5350",
    "red_bright":   "#ff6659",
    "yellow":       "#ffc107",
    "peach":        "#ff9800",
    "mauve":        "#ba68c8",
    "teal":         "#26a69a",
    # Surfaces (for chart gridlines etc.)
    "surface0":     "#162a3a",
    "surface1":     "#1e3a4f",
    "surface2":     "#2a4a5f",
    # Heatmap gradient
    "heatmap_green":  "#2e7d32",
    "heatmap_yellow": "#f9a825",
    "heatmap_red":    "#c62828",
    # Pill buttons
    "pill_active_bg":   "#00b8d4",
    "pill_active_text": "#0a1219",
    "pill_inactive_bg": "#162a3a",
    "pill_inactive_text": "#8899aa",
}

# ---------------------------------------------------------------------------
# Font sizes
# ---------------------------------------------------------------------------
FONTS = {
    "family": "Inter",
    "fallback": "Segoe UI",
    "size_sm": 9,
    "size_md": 10,
    "size_lg": 12,
    "size_xl": 16,
    "size_xxl": 24,
    "size_hero": 32,
}


# ---------------------------------------------------------------------------
# QSS Stylesheet
# ---------------------------------------------------------------------------
def build_stylesheet() -> str:
    c = COLORS
    f = FONTS
    ff = f'"{f["family"]}", "{f["fallback"]}", sans-serif'
    return f"""
    /* ── Global ───────────────────────────────────────── */
    QWidget {{
        background-color: {c['bg_primary']};
        color: {c['text']};
        font-family: {ff};
        font-size: {f['size_md']}pt;
    }}

    /* ── Sidebar ──────────────────────────────────────── */
    QWidget#sidebar {{
        background-color: {c['bg_secondary']};
        border-right: 1px solid {c['border']};
    }}

    QPushButton#navButton {{
        text-align: left;
        padding: 13px 20px;
        border: none;
        border-radius: 14px;
        background-color: transparent;
        color: {c['text_dim']};
        font-size: {f['size_md']}pt;
        font-weight: 600;
        letter-spacing: 0.3px;
        outline: none;
    }}
    QPushButton#navButton:hover {{
        background-color: {c['bg_hover']};
        color: {c['text']};
    }}
    QPushButton#navButton[active="true"] {{
        background-color: {c['accent_dim']};
        color: {c['accent']};
        border-left: 3px solid {c['accent']};
    }}

    /* ── Cards ────────────────────────────────────────── */
    QFrame#statCard {{
        background-color: {c['bg_card']};
        border: 1px solid {c['border']};
        border-radius: 14px;
        padding: 18px;
    }}

    /* ── Scroll Areas ─────────────────────────────────── */
    QScrollArea {{
        border: none;
        background-color: transparent;
    }}
    QScrollArea > QWidget > QWidget {{
        background-color: transparent;
    }}
    QScrollBar:vertical {{
        background: {c['bg_secondary']};
        width: 6px;
        border-radius: 3px;
    }}
    QScrollBar::handle:vertical {{
        background: {c['surface2']};
        border-radius: 3px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {c['accent']};
    }}
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar:horizontal {{
        background: {c['bg_secondary']};
        height: 6px;
        border-radius: 3px;
    }}
    QScrollBar::handle:horizontal {{
        background: {c['surface2']};
        border-radius: 3px;
        min-width: 30px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {c['accent']};
    }}
    QScrollBar::add-line:horizontal,
    QScrollBar::sub-line:horizontal {{
        width: 0px;
    }}

    /* ── Line Edits / Search Bars ─────────────────────── */
    QLineEdit {{
        background-color: {c['bg_input']};
        border: 1px solid {c['border']};
        border-radius: 10px;
        padding: 9px 14px;
        color: {c['text']};
        font-size: {f['size_md']}pt;
        selection-background-color: {c['accent']};
        placeholder-text-color: {c['text_muted']};
    }}
    QLineEdit:focus {{
        border: 1px solid {c['accent']};
    }}

    /* ── ComboBox ──────────────────────────────────────── */
    QComboBox {{
        background-color: {c['bg_input']};
        border: 1px solid {c['border']};
        border-radius: 10px;
        padding: 7px 14px;
        color: {c['text']};
        min-width: 80px;
    }}
    QComboBox:hover {{
        border: 1px solid {c['accent']};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 24px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {c['bg_card']};
        color: {c['text']};
        selection-background-color: {c['bg_hover']};
        selection-color: {c['accent']};
        border: 1px solid {c['border']};
        border-radius: 6px;
    }}

    /* ── Push Buttons ─────────────────────────────────── */
    QPushButton {{
        background-color: {c['bg_hover']};
        border: 2px solid {c['border_light']};
        border-radius: 18px;
        padding: 8px 22px;
        min-height: 20px;
        color: {c['text']};
        font-weight: 600;
        outline: none;
    }}
    QPushButton:hover {{
        background-color: {c['surface2']};
        border-color: {c['accent']};
        color: {c['accent']};
    }}
    QPushButton:pressed {{
        background-color: {c['surface2']};
    }}
    QPushButton:disabled {{
        color: {c['text_muted']};
        background-color: {c['bg_secondary']};
        border-color: {c['border']};
    }}

    QPushButton#accentBtn {{
        background-color: {c['accent']};
        color: #ffffff;
        border: 2px solid {c['accent']};
        border-radius: 18px;
        padding: 8px 22px;
        min-height: 20px;
        font-weight: 700;
        outline: none;
    }}
    QPushButton#accentBtn:hover {{
        background-color: {c['accent_hover']};
        border-color: {c['accent_hover']};
        color: #ffffff;
    }}
    QPushButton#accentBtn:pressed {{
        background-color: {c['accent']};
    }}

    /* ── Pill Buttons (toggle filters) ────────────────── */
    QPushButton#pillBtn {{
        background-color: {c['pill_inactive_bg']};
        color: {c['text']};
        border: 2px solid {c['border_light']};
        border-radius: 18px;
        padding: 7px 18px;
        min-height: 18px;
        font-size: {f['size_sm']}pt;
        font-weight: 600;
        min-width: 40px;
        outline: none;
    }}
    QPushButton#pillBtn:hover {{
        background-color: {c['bg_hover']};
        color: {c['accent']};
        border-color: {c['accent']};
    }}
    QPushButton#pillBtn[active="true"] {{
        background-color: {c['pill_active_bg']};
        color: #ffffff;
        border: 2px solid {c['pill_active_bg']};
        font-weight: 700;
    }}

    /* ── Tables (QTableWidget) ────────────────────────── */
    QTableWidget, QTableView {{
        background-color: {c['bg_secondary']};
        alternate-background-color: {c['bg_card']};
        gridline-color: {c['border']};
        border: none;
        border-radius: 10px;
        color: {c['text']};
        font-size: {f['size_sm']}pt;
        selection-background-color: {c['bg_hover']};
        selection-color: {c['accent']};
    }}
    QHeaderView::section {{
        background-color: {c['bg_card']};
        color: {c['accent']};
        font-weight: 700;
        font-size: {f['size_sm']}pt;
        padding: 8px 10px;
        border: none;
        border-bottom: 2px solid {c['accent']};
        letter-spacing: 0.5px;
    }}

    /* ── Tab Widget (for sub-tabs inside pages) ───────── */
    QTabWidget::pane {{
        border: none;
        background-color: transparent;
    }}
    QTabBar::tab {{
        background-color: {c['bg_secondary']};
        color: {c['text_dim']};
        padding: 10px 20px;
        border: none;
        border-bottom: 2px solid transparent;
        font-weight: 600;
    }}
    QTabBar::tab:selected {{
        color: {c['accent']};
        border-bottom: 2px solid {c['accent']};
    }}
    QTabBar::tab:hover {{
        color: {c['text']};
    }}

    /* ── Progress Bar ─────────────────────────────────── */
    QProgressBar {{
        background-color: {c['bg_secondary']};
        border: none;
        border-radius: 3px;
        height: 4px;
        text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{
        background-color: {c['accent']};
        border-radius: 3px;
    }}

    /* ── Labels ───────────────────────────────────────── */
    QLabel#titleLabel {{
        font-size: {f['size_xxl']}pt;
        font-weight: 800;
        color: {c['text']};
        letter-spacing: -0.5px;
    }}
    QLabel#headerLabel {{
        font-size: {f['size_xl']}pt;
        font-weight: 700;
        color: {c['text']};
    }}
    QLabel#subHeaderLabel {{
        font-size: {f['size_lg']}pt;
        font-weight: 700;
        color: {c['accent']};
        letter-spacing: 0.5px;
    }}
    QLabel#sectionLabel {{
        font-size: {f['size_lg']}pt;
        font-weight: 700;
        color: {c['accent']};
        padding: 8px 0px 4px 0px;
        letter-spacing: 1px;
    }}
    QLabel#dimLabel {{
        color: {c['text_dim']};
    }}
    QLabel#mutedLabel {{
        color: {c['text_muted']};
    }}
    QLabel#valueLabel {{
        font-size: {f['size_lg']}pt;
        font-weight: 700;
        color: {c['accent']};
    }}

    /* ── Separators ───────────────────────────────────── */
    QFrame#separator {{
        background-color: {c['border']};
        max-height: 1px;
    }}

    /* ── WebEngineView (chart container) ──────────────── */
    QWebEngineView {{
        background-color: {c['bg_primary']};
        border-radius: 10px;
    }}

    /* ── Splitter handle ──────────────────────────────── */
    QSplitter::handle {{
        background-color: {c['border']};
        width: 1px;
    }}

    /* ── Splitter ─────────────────────────────────────── */
    QSplitter::handle {{
        background-color: {c['border']};
        width: 1px;
    }}
    """


# ---------------------------------------------------------------------------
# DWM dark title bar (Windows only)
# ---------------------------------------------------------------------------
def enable_dark_title_bar(window):
    """Set the title bar to dark mode on Windows 10+ via DWM API."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(window.winId())
        dwmapi = ctypes.windll.dwmapi
        value = ctypes.c_int(1)
        hr = dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        if hr != 0:
            # Fallback for older Win10 builds
            dwmapi.DwmSetWindowAttribute(
                hwnd, 19, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass
