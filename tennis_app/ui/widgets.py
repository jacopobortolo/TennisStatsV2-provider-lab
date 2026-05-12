"""
Reusable custom widgets for the Tennis Analytics V2 UI.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QLineEdit, QGridLayout, QSizePolicy, QScrollArea,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QPushButton, QListWidget, QListWidgetItem,
)
from PySide6.QtCore import Qt, Signal, QTimer, QThread
from PySide6.QtGui import QColor, QFont, QPalette

from .theme import COLORS, FONTS


# ---------------------------------------------------------------------------
# Horizontal separator
# ---------------------------------------------------------------------------

class Separator(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("separator")
        self.setFrameShape(QFrame.HLine)
        self.setFixedHeight(1)


# ---------------------------------------------------------------------------
# Section Header — uppercase accent label like tennisratio
# ---------------------------------------------------------------------------

class SectionHeader(QLabel):
    """Styled section header with uppercase accent text, like tennisratio sections."""

    def __init__(self, text: str, parent=None):
        super().__init__(text.upper(), parent)
        self.setObjectName("sectionLabel")


# ---------------------------------------------------------------------------
# Pill Button Group — horizontal exclusive-toggle pill buttons
# ---------------------------------------------------------------------------

class PillButtonGroup(QWidget):
    """Horizontal row of pill-shaped toggle buttons (exclusive selection).

    Emits ``changed(str)`` with the selected value.
    """
    changed = Signal(str)

    def __init__(self, options: list[str], default: str | None = None,
                 parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._buttons: dict[str, QPushButton] = {}
        self._selected: str = default or (options[0] if options else "")

        for text in options:
            btn = QPushButton(text)
            btn.setObjectName("pillBtn")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(False)
            btn.clicked.connect(lambda checked=False, t=text: self._on_click(t))
            layout.addWidget(btn)
            self._buttons[text] = btn

        layout.addStretch()
        self._apply_styles()

    def _on_click(self, text: str):
        if text == self._selected:
            return
        self._selected = text
        self._apply_styles()
        self.changed.emit(text)

    def _apply_styles(self):
        for text, btn in self._buttons.items():
            active = text == self._selected
            btn.setProperty("active", active)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def value(self) -> str:
        return self._selected

    def set_value(self, text: str):
        if text in self._buttons:
            self._selected = text
            self._apply_styles()


# ---------------------------------------------------------------------------
# Multi Pill Button Group — multi-select pill buttons with sticky "All" entry
# ---------------------------------------------------------------------------

class MultiPillButtonGroup(QWidget):
    """Horizontal row of pill toggle buttons supporting multi-selection.

    The first option is treated as the "select all / clear" sentinel:
    clicking it deselects every other button.  Selecting any other button
    deselects the sentinel.  When nothing is selected the sentinel turns
    back on.

    Emits ``changed(list[str])`` with the active values (excluding the
    sentinel).  An empty list means "no filter" (sentinel active).
    """
    changed = Signal(list)

    def __init__(self, options: list[str], parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._buttons: dict[str, QPushButton] = {}
        self._all_label: str = options[0] if options else "All"
        # Active set excludes the sentinel; empty means "all"
        self._selected: set[str] = set()

        for text in options:
            btn = QPushButton(text)
            btn.setObjectName("pillBtn")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(False)
            btn.clicked.connect(lambda checked=False, t=text: self._on_click(t))
            layout.addWidget(btn)
            self._buttons[text] = btn

        layout.addStretch()
        self._apply_styles()

    def _on_click(self, text: str):
        if text == self._all_label:
            if not self._selected:
                return  # already in "all" state
            self._selected.clear()
        else:
            if text in self._selected:
                self._selected.discard(text)
            else:
                self._selected.add(text)
        self._apply_styles()
        self.changed.emit(self.values())

    def _apply_styles(self):
        all_active = not self._selected
        for text, btn in self._buttons.items():
            active = (text == self._all_label and all_active) \
                or (text != self._all_label and text in self._selected)
            btn.setProperty("active", active)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def values(self) -> list[str]:
        """Return active selections (sentinel excluded). Empty = no filter."""
        return sorted(self._selected)

    def set_values(self, items: list[str]):
        self._selected = {x for x in items if x in self._buttons
                          and x != self._all_label}
        self._apply_styles()


# ---------------------------------------------------------------------------
# Stat Card — small metric box (icon + label + value)
# ---------------------------------------------------------------------------

class StatCard(QFrame):
    """A rounded card showing an optional icon, label, and a large value."""

    def __init__(self, label: str, value: str, parent=None,
                 accent_color=None, icon: str = ""):
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(4)

        # Top row: icon + label
        top = QHBoxLayout()
        top.setSpacing(6)

        if icon:
            icon_lbl = QLabel(icon)
            icon_lbl.setStyleSheet(
                f"font-size: {FONTS['size_xl']}pt; background: transparent;")
            top.addWidget(icon_lbl)

        lbl = QLabel(label)
        lbl.setObjectName("dimLabel")
        lbl.setStyleSheet("background: transparent;")
        top.addWidget(lbl)
        top.addStretch()

        layout.addLayout(top)

        # Value
        self._value_label = QLabel(value)
        color = accent_color or COLORS["accent"]
        self._value_label.setStyleSheet(
            f"font-size: {FONTS['size_xl']}pt; font-weight: 700; "
            f"color: {color}; background: transparent;")
        layout.addWidget(self._value_label)

    def update_value(self, value: str):
        self._value_label.setText(value)


# ---------------------------------------------------------------------------
# Player Header — name + info line
# ---------------------------------------------------------------------------

class PlayerHeader(QWidget):
    """Large player name header with info line underneath."""

    def __init__(self, name: str, info_parts: list[str], parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        name_label = QLabel(name)
        name_label.setObjectName("titleLabel")
        layout.addWidget(name_label)

        if info_parts:
            info_label = QLabel("  ·  ".join(info_parts))
            info_label.setObjectName("dimLabel")
            layout.addWidget(info_label)


# ---------------------------------------------------------------------------
# Search Bar — line edit with optional button
# ---------------------------------------------------------------------------

class SearchBar(QWidget):
    """Search input with an optional button to the right."""
    searched = Signal(str)

    def __init__(self, placeholder="Search...", button_text=None, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.line_edit = QLineEdit()
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.returnPressed.connect(self._on_submit)
        layout.addWidget(self.line_edit, 1)

        if button_text:
            btn = QPushButton(button_text)
            btn.setObjectName("accentBtn")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(self._on_submit)
            layout.addWidget(btn)

    def _on_submit(self):
        self.searched.emit(self.line_edit.text().strip())

    def text(self):
        return self.line_edit.text().strip()

    def set_text(self, text: str):
        self.line_edit.setText(text)


# ---------------------------------------------------------------------------
# Player Search Edit — line edit with autocomplete dropdown
# ---------------------------------------------------------------------------

class PlayerSearchEdit(QWidget):
    """Line edit with a floating dropdown that shows matching players as you type.

    Requires a ``db`` object with a ``search_players(query, limit)`` method.
    Emits ``player_selected(dict)`` when the user picks a player from the list.
    """
    player_selected = Signal(object)  # emits the player dict

    class _SearchWorker(QThread):
        results_ready = Signal(list)

        def __init__(self, db, query, parent=None):
            super().__init__(parent)
            self._db = db
            self._query = query

        def run(self):
            try:
                results = self._db.search_players(self._query, limit=10)
            except Exception:
                results = []
            self.results_ready.emit(results)

    def __init__(self, db, placeholder="Player name...", parent=None):
        super().__init__(parent)
        self._db = db
        self._players: list[dict] = []
        self._search_worker = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.line_edit = QLineEdit()
        self.line_edit.setPlaceholderText(placeholder)
        layout.addWidget(self.line_edit)

        # Floating popup list
        self._popup = QListWidget()
        self._popup.setWindowFlags(Qt.ToolTip)
        self._popup.setStyleSheet(
            f"QListWidget {{ background: {COLORS['bg_card']}; color: {COLORS['text']};"
            f" border: 1px solid {COLORS['accent']}; border-radius: 6px;"
            f" font-size: {FONTS['size_md']}pt; padding: 4px; }}"
            f" QListWidget::item {{ padding: 6px 10px; }}"
            f" QListWidget::item:hover {{ background: {COLORS['bg_hover']}; }}"
            f" QListWidget::item:selected {{ background: {COLORS['accent']};"
            f" color: #ffffff; }}"
        )
        self._popup.setMaximumHeight(220)
        self._popup.hide()

        self._popup.itemClicked.connect(self._on_item_clicked)
        self.line_edit.textChanged.connect(self._on_text_changed)
        self.line_edit.returnPressed.connect(self._on_return)

        # Debounce timer (200ms)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._do_search)

    def _on_text_changed(self, text: str):
        if len(text.strip()) < 2:
            self._popup.hide()
            return
        self._timer.start()

    def _do_search(self):
        query = self.line_edit.text().strip()
        if len(query) < 2:
            self._popup.hide()
            return
        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.quit()
            self._search_worker.wait(200)
        worker = self._SearchWorker(self._db, query, parent=self)
        worker.results_ready.connect(self._on_search_results)
        self._search_worker = worker
        worker.start()

    def _on_search_results(self, results):
        # Guard: text might have changed by the time results arrive
        current_query = self.line_edit.text().strip()
        if len(current_query) < 2:
            self._popup.hide()
            return
        self._players = results
        self._popup.clear()
        if not results:
            self._popup.hide()
            return
        for p in results:
            name = f"{p['name_first']} {p['name_last']}"
            country = p.get("ioc", "")
            item = QListWidgetItem(f"{name}  ({country})" if country else name)
            self._popup.addItem(item)
        # Position popup below the line edit
        pos = self.line_edit.mapToGlobal(self.line_edit.rect().bottomLeft())
        self._popup.setFixedWidth(self.line_edit.width())
        self._popup.move(pos)
        self._popup.show()

    def _on_item_clicked(self, item: QListWidgetItem):
        idx = self._popup.row(item)
        if 0 <= idx < len(self._players):
            player = self._players[idx]
            name = f"{player['name_first']} {player['name_last']}"
            self.line_edit.blockSignals(True)
            self.line_edit.setText(name)
            self.line_edit.blockSignals(False)
            self._popup.hide()
            self.player_selected.emit(player)

    def _on_return(self):
        """If popup is visible and has items, pick the first one; else emit top match."""
        if self._popup.isVisible() and self._popup.count() > 0:
            self._popup.setCurrentRow(0)
            self._on_item_clicked(self._popup.item(0))
        elif self._players:
            self._on_item_clicked(self._popup.item(0) if self._popup.count() else None)

    def text(self) -> str:
        return self.line_edit.text().strip()

    def set_text(self, text: str):
        self.line_edit.blockSignals(True)
        self.line_edit.setText(text)
        self.line_edit.blockSignals(False)

    def selected_player(self) -> dict | None:
        """Return the last selected player dict, or None."""
        # Check if current text matches any cached player
        current = self.text().lower()
        for p in self._players:
            name = f"{p['name_first']} {p['name_last']}".lower()
            if name == current:
                return p
        return None


# ---------------------------------------------------------------------------
# Filter Bar — horizontal row of combo-box filters
# ---------------------------------------------------------------------------

class FilterBar(QWidget):
    """Horizontal bar of labeled combo boxes."""
    changed = Signal()

    def __init__(self, filters: dict, parent=None):
        """
        filters: dict of {label: [option_values]}
        """
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self._combos = {}

        from PySide6.QtWidgets import QComboBox

        for label, options in filters.items():
            lbl = QLabel(label + ":")
            lbl.setObjectName("dimLabel")
            layout.addWidget(lbl)

            combo = QComboBox()
            combo.addItems(options)
            combo.currentIndexChanged.connect(lambda _: self.changed.emit())
            layout.addWidget(combo)
            self._combos[label] = combo

        layout.addStretch()

    def value(self, label: str) -> str:
        combo = self._combos.get(label)
        return combo.currentText() if combo else ""

    def set_values(self, label: str, options: list[str]):
        combo = self._combos.get(label)
        if combo:
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(options)
            combo.blockSignals(False)


# ---------------------------------------------------------------------------
# Stat Grid — grid of StatCards with optional section title
# ---------------------------------------------------------------------------

class StatGrid(QWidget):
    """A responsive grid of StatCards."""

    def __init__(self, parent=None, columns=4, title: str = ""):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        if title:
            header = SectionHeader(title)
            outer.addWidget(header)

        self._columns = columns
        self._grid_widget = QWidget()
        self._layout = QGridLayout(self._grid_widget)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)
        self._row = 0
        self._col = 0
        outer.addWidget(self._grid_widget)

    def add_stat(self, label: str, value: str, accent_color=None,
                 icon: str = ""):
        card = StatCard(label, value, accent_color=accent_color, icon=icon)
        self._layout.addWidget(card, self._row, self._col)
        self._col += 1
        if self._col >= self._columns:
            self._col = 0
            self._row += 1
        return card

    def clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._row = 0
        self._col = 0


# ---------------------------------------------------------------------------
# DataTable — styled QTableWidget wrapper
# ---------------------------------------------------------------------------

class DataTable(QTableWidget):
    """Pre-styled table widget with dark theme and alternating rows."""

    def __init__(self, columns: list[tuple[str, int]], parent=None):
        """
        columns: list of (header_text, width) tuples.
        """
        super().__init__(parent)
        self.setColumnCount(len(columns))
        headers = []
        for i, (text, width) in enumerate(columns):
            headers.append(text)
            self.setColumnWidth(i, width)
        self.setHorizontalHeaderLabels(headers)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)

    def populate(self, rows: list[list[str]], color_rules: dict = None):
        """
        Fill the table with string data.
        color_rules: dict of column_index -> callable(value) -> QColor or None
        """
        self.setUpdatesEnabled(False)
        self.blockSignals(True)
        try:
            self.setRowCount(len(rows))
            for r, row_data in enumerate(rows):
                for c, value in enumerate(row_data):
                    item = QTableWidgetItem(str(value))
                    item.setTextAlignment(Qt.AlignCenter)
                    if color_rules and c in color_rules:
                        color = color_rules[c](value)
                        if color:
                            item.setForeground(color)
                    self.setItem(r, c, item)
        finally:
            self.blockSignals(False)
            self.setUpdatesEnabled(True)


# ---------------------------------------------------------------------------
# HeatmapTable — DataTable with cell-level color gradients
# ---------------------------------------------------------------------------

def _heatmap_color(value: float, min_val: float, max_val: float,
                   invert: bool = False) -> QColor:
    """Return a QColor on a green→yellow→red gradient based on position."""
    if max_val == min_val:
        return QColor(COLORS["heatmap_yellow"])
    ratio = max(0.0, min(1.0, (value - min_val) / (max_val - min_val)))
    if invert:
        ratio = 1.0 - ratio
    # green (high) → yellow (mid) → red (low)
    if ratio >= 0.5:
        # green → yellow
        t = (ratio - 0.5) * 2
        r = int(249 * (1 - t) + 46 * t)
        g = int(168 * (1 - t) + 125 * t)
        b = int(37 * (1 - t) + 50 * t)
    else:
        # yellow → red
        t = ratio * 2
        r = int(198 * (1 - t) + 249 * t)
        g = int(40 * (1 - t) + 168 * t)
        b = int(40 * (1 - t) + 37 * t)
    return QColor(r, g, b)


class HeatmapTable(DataTable):
    """DataTable that colors cell backgrounds based on value percentile.

    heatmap_columns: dict mapping column_index → (min_val, max_val)
        or column_index → (min_val, max_val, invert).
    """

    def __init__(self, columns: list[tuple[str, int]],
                 heatmap_columns: dict | None = None, parent=None):
        super().__init__(columns, parent)
        self._heatmap_columns = heatmap_columns or {}

    def set_heatmap_columns(self, heatmap_columns: dict):
        self._heatmap_columns = heatmap_columns

    def populate(self, rows: list[list[str]], color_rules: dict = None):
        super().populate(rows, color_rules)
        if not self._heatmap_columns:
            return
        for r in range(self.rowCount()):
            for c, spec in self._heatmap_columns.items():
                item = self.item(r, c)
                if not item:
                    continue
                try:
                    val = float(item.text().replace("%", "").replace(",", ""))
                except (ValueError, TypeError):
                    continue
                invert = spec[2] if len(spec) > 2 else False
                bg = _heatmap_color(val, spec[0], spec[1], invert)
                bg.setAlpha(140)
                item.setBackground(bg)
                item.setForeground(QColor("#ffffff"))


# ---------------------------------------------------------------------------
# Comparison Bar — side-by-side H2H stat bar
# ---------------------------------------------------------------------------

class ComparisonBar(QWidget):
    """Side-by-side horizontal bar for comparing two values.

    Layout: [stat_label] [p1_value] [bar1 | bar2] [p2_value]
    Labels are left-aligned for readability.
    """

    def __init__(self, stat_label: str, p1_value: float, p2_value: float,
                 fmt: str = ".1f", p1_color: str | None = None,
                 p2_color: str | None = None, parent=None):
        super().__init__(parent)
        self.setFixedHeight(38)

        c1 = p1_color or COLORS["accent"]
        c2 = p2_color or COLORS["red"]

        total = abs(p1_value) + abs(p2_value) if (p1_value or p2_value) else 1
        p1_pct = abs(p1_value) / total * 100 if total else 50
        p2_pct = 100 - p1_pct

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Stat label — left-aligned
        lbl = QLabel(stat_label)
        lbl.setStyleSheet(
            f"color: {COLORS['text_dim']}; font-size: {FONTS['size_sm']}pt;"
            f" font-weight: 600; background: transparent;")
        lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl.setFixedWidth(130)
        layout.addWidget(lbl)

        # P1 value
        v1 = QLabel(f"{p1_value:{fmt}}")
        v1.setStyleSheet(
            f"color: {c1}; font-weight: 700; font-size: {FONTS['size_md']}pt;"
            f" min-width: 50px; background: transparent;")
        v1.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(v1)

        # Bar container
        bar_container = QWidget()
        bar_container.setFixedHeight(24)
        bar_container.setStyleSheet(
            f"background: {COLORS['bg_card']}; border-radius: 12px;")
        bar_layout = QHBoxLayout(bar_container)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(0)

        bar1 = QFrame()
        bar1.setStyleSheet(
            f"background: {c1}; border-radius: 12px;"
            f" border-top-right-radius: 0px; border-bottom-right-radius: 0px;")
        bar_layout.addWidget(bar1, int(p1_pct))

        bar2 = QFrame()
        bar2.setStyleSheet(
            f"background: {c2}; border-radius: 12px;"
            f" border-top-left-radius: 0px; border-bottom-left-radius: 0px;")
        bar_layout.addWidget(bar2, int(p2_pct))

        layout.addWidget(bar_container, 1)

        # P2 value
        v2 = QLabel(f"{p2_value:{fmt}}")
        v2.setStyleSheet(
            f"color: {c2}; font-weight: 700; font-size: {FONTS['size_md']}pt;"
            f" min-width: 50px; background: transparent;")
        v2.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(v2)


# ---------------------------------------------------------------------------
# Scrollable Page — base widget for all pages
# ---------------------------------------------------------------------------

class ScrollablePage(QScrollArea):
    """A scroll area wrapping a content widget, used as base for pages."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)

        # Force dark viewport so no white flash during content swap
        pal = self.viewport().palette()
        pal.setColor(QPalette.Window, QColor(COLORS["bg_primary"]))
        self.viewport().setPalette(pal)
        self.viewport().setAutoFillBackground(True)

        self._content = QWidget()
        self._content.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(28, 24, 28, 24)
        self._layout.setSpacing(16)
        self._layout.setAlignment(Qt.AlignTop)
        self.setWidget(self._content)

        self._pending_content = None
        self._pending_layout = None

    @property
    def content_layout(self) -> QVBoxLayout:
        if self._pending_layout is not None:
            return self._pending_layout
        return self._layout

    def begin_update(self):
        """Start building new content off-screen. Widgets added to
        content_layout will go into the pending buffer."""
        self._pending_content = QWidget()
        self._pending_content.setStyleSheet("background: transparent;")
        self._pending_layout = QVBoxLayout(self._pending_content)
        self._pending_layout.setContentsMargins(28, 24, 28, 24)
        self._pending_layout.setSpacing(16)
        self._pending_layout.setAlignment(Qt.AlignTop)
        return self._pending_layout

    def end_update(self):
        """Swap the pending buffer in, replacing old content atomically."""
        if self._pending_content is None:
            return
        old = self._content
        self._content = self._pending_content
        self._layout = self._pending_layout
        self._pending_content = None
        self._pending_layout = None
        # Hide old widget before swap to avoid bare-viewport flash
        old.hide()
        self.takeWidget()
        self.setWidget(self._content)
        try:
            old.setParent(None)
            old.deleteLater()
        except RuntimeError:
            pass  # already deleted by setWidget

    def clear_content(self):
        """Remove all widgets from the content layout."""
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            elif item.layout():
                _clear_layout(item.layout())


def _clear_layout(layout):
    """Recursively clear a layout."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w:
            w.deleteLater()
        elif item.layout():
            _clear_layout(item.layout())
