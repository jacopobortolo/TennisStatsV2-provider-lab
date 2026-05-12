"""
Tournaments browser page.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QSplitter, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QBrush

from ..widgets import SearchBar, DataTable, Separator, PillButtonGroup
from ..theme import COLORS
from ..match_detail_dialog import MatchDetailDialog


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class _TournamentListWorker(QThread):
    """Fetch tournament list off the UI thread."""
    finished = Signal(list)

    def __init__(self, db, year, tour, is_doubles, parent=None):
        super().__init__(parent)
        self._db = db
        self._year = year
        self._tour = tour
        self._is_doubles = is_doubles

    def run(self):
        try:
            if self._is_doubles:
                result = self._db.get_doubles_tournament_list(
                    year=self._year, tour=self._tour)
            else:
                result = self._db.get_tournament_list(
                    year=self._year, tour=self._tour)
        except Exception:
            result = []
        self.finished.emit(result)


class _TournamentDrawWorker(QThread):
    """Fetch tournament draw/results off the UI thread."""
    finished = Signal(list)

    def __init__(self, db, tourney_name, year, tour, is_doubles, parent=None):
        super().__init__(parent)
        self._db = db
        self._tourney_name = tourney_name
        self._year = year
        self._tour = tour
        self._is_doubles = is_doubles

    def run(self):
        try:
            if self._is_doubles:
                result = self._db.get_doubles_tournament_results(
                    tourney_name=self._tourney_name,
                    year=self._year, tour=self._tour)
            else:
                result = self._db.get_tournament_results(
                    tourney_name=self._tourney_name,
                    year=self._year, tour=self._tour)
        except Exception:
            result = []
        self.finished.emit(result)


class _YearsWorker(QThread):
    """Fetch available years off the UI thread."""
    finished = Signal(list)

    def __init__(self, db, tour, parent=None):
        super().__init__(parent)
        self._db = db
        self._tour = tour

    def run(self):
        try:
            result = self._db.get_available_years(tour=self._tour)
        except Exception:
            result = []
        self.finished.emit(result)

ROUND_ORDER = {
    "Q1": 0, "Q2": 1, "Q3": 2,
    "R128": 3, "R64": 4, "R32": 5, "R16": 6,
    "QF": 7, "SF": 8, "F": 9, "RR": 10, "ER": 11, "BR": 12,
}


class TournamentsPage(QWidget):
    """Page for browsing tournament draws and results."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._tourney_cache = {}
        self._current_draw_matches = []
        self._list_worker = None
        self._draw_worker = None
        self._years_worker = None
        self._first_show = True
        self._pending_query = None  # query string to apply after list loads
        self._pending_nav = None    # (tourney_name, year_str) set by navigate_to()
        self._build_ui()

    def showEvent(self, event):
        """Defer first data load until the page is actually visible."""
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            self._on_tour_change()

    def _on_tour_change(self):
        """Reload the year dropdown for the currently selected tour."""
        tour = self.tour_pills.value().lower()
        if self._years_worker and self._years_worker.isRunning():
            self._years_worker.quit()
            self._years_worker.wait(500)
        worker = _YearsWorker(self.db, tour, parent=self)
        worker.finished.connect(self._on_years_loaded)
        self._years_worker = worker
        worker.start()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        header = QLabel("Tournaments")
        header.setObjectName("headerLabel")
        layout.addWidget(header)

        # --- Filters ---
        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)

        self.search_bar = SearchBar(
            placeholder="Tournament name...", button_text="🔍 Search")
        self.search_bar.searched.connect(self._search)
        filter_row.addWidget(self.search_bar)

        self.tour_pills = PillButtonGroup(["ATP", "WTA"])
        self.tour_pills.changed.connect(self._on_tour_change)
        filter_row.addWidget(self.tour_pills)

        self.type_pills = PillButtonGroup(["Singles", "Doubles"])
        filter_row.addWidget(self.type_pills)

        filter_row.addWidget(QLabel("Year:"))
        self.year_combo = QComboBox()
        # Years populated lazily on first showEvent via _on_tour_change
        self.year_combo.currentIndexChanged.connect(lambda _: self._search())
        filter_row.addWidget(self.year_combo)

        filter_row.addStretch()
        layout.addLayout(filter_row)

        # --- Splitter: tournament list | draw ---
        splitter = QSplitter(Qt.Horizontal)

        # Left: tournament list
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)

        lbl = QLabel("Tournaments")
        lbl.setObjectName("subHeaderLabel")
        left_layout.addWidget(lbl)

        self.tourney_table = DataTable([
            ("Tournament", 170), ("Surface", 70), ("Level", 70),
        ])
        self.tourney_table.cellClicked.connect(self._on_tourney_select)
        left_layout.addWidget(self.tourney_table)

        splitter.addWidget(left)

        # Right: draw
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 0, 0, 0)

        self.draw_header = QLabel("Select a tournament")
        self.draw_header.setObjectName("subHeaderLabel")
        right_layout.addWidget(self.draw_header)

        self.draw_table = DataTable([
            ("Round", 90), ("Winner", 180), ("Loser", 180),
            ("Score", 160), ("W Rank", 60), ("L Rank", 60),
        ])
        self.draw_table.cellClicked.connect(self._on_draw_cell_click)
        self.draw_table.doubleClicked.connect(self._on_draw_double_clicked)
        right_layout.addWidget(self.draw_table)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        layout.addWidget(splitter, 1)

    def navigate_to(self, tourney_name: str, year_str: str, tour: str):
        """Switch to this tour+year and search for tourney_name."""
        self._first_show = False  # prevent showEvent from triggering a second load
        tour_label = "ATP" if tour.lower() == "atp" else "WTA"
        self.tour_pills.set_value(tour_label)  # silent — no changed signal
        self._pending_nav = (tourney_name, year_str)
        self._on_tour_change()  # loads years, then applies _pending_nav

    def _on_years_loaded(self, years):
        years_sorted = sorted(years, reverse=True)
        self.year_combo.blockSignals(True)
        self.year_combo.clear()
        self.year_combo.addItem("All")
        self.year_combo.addItems([str(y) for y in years_sorted])
        if self._pending_nav:
            tourney_name, year_str = self._pending_nav
            self._pending_nav = None
            idx = self.year_combo.findText(year_str)
            if idx >= 0:
                self.year_combo.setCurrentIndex(idx)
            self.year_combo.blockSignals(False)
            self.search_bar.set_text(tourney_name)
            self._auto_select = True
            self._search(tourney_name)
        else:
            self.year_combo.blockSignals(False)
            self._search()

    def _search(self, query: str = ""):
        year_text = self.year_combo.currentText()
        year = int(year_text) if year_text and year_text != "All" else None
        tour = self.tour_pills.value().lower()
        is_doubles = self.type_pills.value() == "Doubles"
        self._pending_query = query

        if self._list_worker and self._list_worker.isRunning():
            self._list_worker.quit()
            self._list_worker.wait(500)

        worker = _TournamentListWorker(
            self.db, year, tour, is_doubles, parent=self)
        worker.finished.connect(self._on_list_loaded)
        self._list_worker = worker
        worker.start()

    def _on_list_loaded(self, tournaments):
        query = self._pending_query or ""
        if query:
            tournaments = [
                t for t in tournaments
                if query.lower() in (t.get("tourney_name", "") or "").lower()
            ]

        # Deduplicate
        seen = set()
        unique = []
        for t in tournaments:
            name = t.get("tourney_name", "")
            if name not in seen:
                seen.add(name)
                unique.append(t)
        tournaments = unique

        level_names = {
            "G": "Grand Slam", "M": "Masters", "A": "ATP",
            "D": "Davis Cup", "F": "Finals", "C": "Challenger",
        }

        self._tourney_cache.clear()
        rows = []
        for i, t in enumerate(tournaments):
            name = t.get("tourney_name", "")
            self._tourney_cache[i] = name
            rows.append([
                name,
                t.get("surface", ""),
                level_names.get(
                    t.get("tourney_level", ""), t.get("tourney_level", "")),
            ])
        self.tourney_table.populate(rows)

        if getattr(self, "_auto_select", False) and rows:
            self._auto_select = False
            self._on_tourney_select(0, 0)

    def _on_tourney_select(self, row, col):
        tourney_name = self._tourney_cache.get(row)
        if not tourney_name:
            return

        year_text = self.year_combo.currentText()
        year = int(year_text) if year_text and year_text != "All" else None
        tour = self.tour_pills.value().lower()
        is_doubles = self.type_pills.value() == "Doubles"

        self.draw_header.setText(f"{tourney_name} ({year_text}) — Loading...")
        self.draw_table.setRowCount(0)

        if self._draw_worker and self._draw_worker.isRunning():
            self._draw_worker.quit()
            self._draw_worker.wait(500)

        worker = _TournamentDrawWorker(
            self.db, tourney_name, year, tour, is_doubles, parent=self)
        worker.finished.connect(
            lambda matches, tn=tourney_name, yt=year_text, id_=is_doubles:
                self._on_draw_loaded(matches, tn, yt, id_))
        self._draw_worker = worker
        worker.start()

    def _on_draw_loaded(self, matches, tourney_name, year_text, is_doubles):
        self.draw_header.setText(f"{tourney_name} ({year_text})")
        matches.sort(
            key=lambda m: (
                ROUND_ORDER.get(m.get("round", ""), 99),
                -(int(m.get("match_num") or 0)),
            )
        )

        # Store for double-click handler
        self._current_draw_matches = list(matches)
        round_names = {
            "F": "Final", "SF": "Semi-Final", "QF": "Quarter-Final",
            "R16": "R16", "R32": "R32", "R64": "R64", "R128": "R128",
            "RR": "Round Robin", "Q3": "Q3", "Q2": "Q2", "Q1": "Q1",
        }

        rows = []
        for m in matches:
            rnd = m.get("round", "")
            if is_doubles:
                w1 = m.get("winner1_name", "") or ""
                w2 = m.get("winner2_name", "") or ""
                l1 = m.get("loser1_name", "") or ""
                l2 = m.get("loser2_name", "") or ""
                winner = f"{w1} / {w2}" if w2 else w1
                loser = f"{l1} / {l2}" if l2 else l1
                w_rank = (str(int(m["winner1_rank"]))
                          if m.get("winner1_rank") else "")
                l_rank = (str(int(m["loser1_rank"]))
                          if m.get("loser1_rank") else "")
            else:
                winner = m.get("winner_name", "")
                loser = m.get("loser_name", "")
                w_rank = (str(int(m["winner_rank"]))
                          if m.get("winner_rank") else "")
                l_rank = (str(int(m["loser_rank"]))
                          if m.get("loser_rank") else "")

            rows.append([
                round_names.get(rnd, rnd),
                winner, loser,
                m.get("score", ""),
                w_rank, l_rank,
            ])
        self.draw_table.populate(rows)

    def _on_draw_cell_click(self, row, col):
        """Highlight all rows containing the clicked player name."""
        item = self.draw_table.item(row, col)
        if not item:
            return
        clicked_text = item.text().strip()
        if not clicked_text or col not in (1, 2):  # Winner / Loser columns
            return

        # Colors for highlighting
        highlight_bg = QColor(COLORS["accent"])
        highlight_bg.setAlpha(40)
        strong_bg = QColor(COLORS["accent"])
        strong_bg.setAlpha(90)
        default_fg = QColor(COLORS["text"])
        accent_fg = QColor(COLORS["accent"])

        for r in range(self.draw_table.rowCount()):
            winner_item = self.draw_table.item(r, 1)
            loser_item = self.draw_table.item(r, 2)
            winner_text = winner_item.text().strip() if winner_item else ""
            loser_text = loser_item.text().strip() if loser_item else ""

            player_in_row = (clicked_text in winner_text) or (clicked_text in loser_text)

            for c in range(self.draw_table.columnCount()):
                cell = self.draw_table.item(r, c)
                if not cell:
                    continue
                if player_in_row:
                    cell_text = cell.text().strip()
                    if clicked_text in cell_text:
                        # Strong highlight on the player's own cell
                        cell.setBackground(QBrush(strong_bg))
                        cell.setForeground(accent_fg)
                    else:
                        # Subtle highlight on the rest of the row
                        cell.setBackground(QBrush(highlight_bg))
                        cell.setForeground(default_fg)
                else:
                    # Reset non-matching rows
                    cell.setBackground(QBrush(QColor(0, 0, 0, 0)))
                    cell.setForeground(default_fg)

    def _on_draw_double_clicked(self, index):
        row = index.row()
        if 0 <= row < len(self._current_draw_matches):
            match = self._current_draw_matches[row]
            # Doubles matches don't have a single winner_name; skip dialog
            if match.get("winner_name") or match.get("winner1_name"):
                dlg = MatchDetailDialog(self.db, match, parent=self)
                dlg.exec()
