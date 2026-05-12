"""
Match History page.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QSpinBox, QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QColor, QBrush

from ..widgets import (
    SearchBar, DataTable, Separator, PillButtonGroup,
    MultiPillButtonGroup, PlayerSearchEdit,
)
from ..theme import COLORS
from ..match_detail_dialog import MatchDetailDialog


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class _MatchesWorker(QThread):
    """Fetch matches + upcoming banner data off the UI thread."""
    data_ready = Signal(list, object)  # (matches, upcoming_row_or_None)
    error = Signal(str)

    def __init__(self, db, player_id, player_name, tour,
                 surface, tourney_level, year, rounds,
                 filter_historical_olympics=False, parent=None):
        super().__init__(parent)
        self._db = db
        self._player_id = player_id
        self._player_name = player_name
        self._tour = tour
        self._surface = surface
        self._tourney_level = tourney_level
        self._year = year
        self._rounds = rounds
        self._filter_historical_olympics = filter_historical_olympics

    def run(self):
        try:
            matches = self._db.get_player_matches(
                self._player_id,
                surface=self._surface,
                tourney_level=self._tourney_level,
                year=self._year,
                round_=self._rounds,
                tour=self._tour,
            )
            if self._filter_historical_olympics:
                # Keep 'A' only when tourney_name contains 'lympic'
                matches = [
                    m for m in matches
                    if m.get("tourney_level") != "A"
                    or "lympic" in (m.get("tourney_name") or "")
                ]
            upcoming = None
            if self._player_name:
                try:
                    upcoming = self._db.get_player_upcoming_match(
                        self._player_name)
                except Exception:
                    pass
            self.data_ready.emit(matches, upcoming)
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

class MatchesPage(QWidget):
    """Page for browsing match history with filters and pagination."""

    # Emitted when the user double-clicks a tournament cell;
    # carries (tourney_name, year_str, tour)
    navigate_to_tournament = Signal(str, str, str)

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._all_matches = []
        self._current_player = None
        self._current_player_id = None
        self._current_player_tour = None
        self._current_page = 1
        self._matches_worker = None
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(250)
        self._filter_timer.timeout.connect(self._refetch_matches)
        self._build_ui()
        self._connect_filters()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # --- Header row with search ---
        header_row = QHBoxLayout()
        header = QLabel("Match History")
        header.setObjectName("headerLabel")
        header_row.addWidget(header)

        self.player_search = PlayerSearchEdit(self.db, placeholder="Player name...")
        self.player_search.setMinimumWidth(320)
        self.player_search.player_selected.connect(self._on_player_selected)
        header_row.addWidget(self.player_search)

        header_row.addStretch()
        layout.addLayout(header_row)

        # --- Filters ---
        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)

        self.surface_pills = PillButtonGroup(
            ["All", "Hard", "Clay", "Grass", "Carpet"])
        filter_row.addWidget(self.surface_pills)

        filter_row.addWidget(QLabel("Year:"))
        self.year_combo = QComboBox()
        self.year_combo.addItems(
            ["All"] + [str(y) for y in range(2026, 1967, -1)])
        filter_row.addWidget(self.year_combo)

        filter_row.addWidget(QLabel("Round:"))
        self.round_pills = MultiPillButtonGroup(
            ["All", "F", "SF", "QF", "R16", "R32", "R64", "R128",
             "RR", "Q3", "Q2", "Q1"])
        filter_row.addWidget(self.round_pills, 1)

        filter_row.addWidget(QLabel("Rows:"))
        self.rows_combo = QComboBox()
        self.rows_combo.addItems(["50", "100", "200", "500", "All"])
        self.rows_combo.currentIndexChanged.connect(
            lambda _: self._display_page())
        filter_row.addWidget(self.rows_combo)

        filter_row.addStretch()
        layout.addLayout(filter_row)

        # --- Upcoming match banner ---
        self.upcoming_label = QLabel("")
        self.upcoming_label.setObjectName("upcomingBanner")
        self.upcoming_label.setStyleSheet(
            "QLabel#upcomingBanner {"
            f"background-color: {COLORS['accent_dim']};"
            f"color: {COLORS['accent']};"
            f"border: 1px solid {COLORS['accent']};"
            "padding: 8px 12px; border-radius: 6px; font-weight: 600;}"
        )
        self.upcoming_label.setVisible(False)
        layout.addWidget(self.upcoming_label)

        # --- Rank filter row ---
        rank_row = QHBoxLayout()
        rank_row.setSpacing(10)
        rank_row.addWidget(QLabel("Opp. rank:"))
        self.rank_pills = PillButtonGroup(
            ["All", "Top 5", "Top 10", "Top 20", "Top 50", "Top 100"])
        rank_row.addWidget(self.rank_pills)

        rank_row.addWidget(QLabel("Tournament Level:"))
        self.level_pills = MultiPillButtonGroup(
            ["All", "GS", "M", "ATP/WTA", "CH",
             "DC/BJKC", "Fin", "Oly"])
        rank_row.addWidget(self.level_pills, 1)
        rank_row.addStretch()
        layout.addLayout(rank_row)

        table_header_row = QHBoxLayout()
        table_header_row.setContentsMargins(0, 0, 0, 0)
        table_header_row.addStretch()
        self.wl_label = QLabel("")
        self.wl_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.wl_label.setStyleSheet(
            f"color: {COLORS['accent']}; font-size: 14pt; font-weight: 800;"
        )
        table_header_row.addWidget(self.wl_label)
        layout.addLayout(table_header_row)

        # --- Table ---
        self.table = DataTable([
            ("Date", 90), ("Tournament", 170), ("Level", 80),
            ("Surface", 65), ("Round", 55), ("Winner", 160),
            ("Loser", 160), ("W Rk", 50), ("L Rk", 50),
            ("Score", 210), ("Min", 45),
        ])
        self.table.doubleClicked.connect(self._on_match_double_clicked)
        layout.addWidget(self.table, 1)

        # --- Pagination ---
        page_row = QHBoxLayout()
        self.status_label = QLabel(
            "Enter a player name and click Search")
        self.status_label.setObjectName("dimLabel")
        page_row.addWidget(self.status_label)
        page_row.addStretch()

        self.prev_btn = QPushButton("◀ Prev")
        self.prev_btn.setObjectName("accentBtn")
        self.prev_btn.setCursor(Qt.PointingHandCursor)
        self.prev_btn.clicked.connect(self._prev_page)
        page_row.addWidget(self.prev_btn)

        self.page_label = QLabel("Page 1")
        self.page_label.setObjectName("dimLabel")
        page_row.addWidget(self.page_label)

        self.next_btn = QPushButton("Next ▶")
        self.next_btn.setObjectName("accentBtn")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.clicked.connect(self._next_page)
        page_row.addWidget(self.next_btn)

        layout.addLayout(page_row)

    def _on_player_selected(self, player):
        """Called when user selects a player from autocomplete."""
        self._current_player_id = player["player_id"]
        self._current_player_tour = player.get("tour")
        self._current_player = f"{player['name_first']} {player['name_last']}"
        self._refetch_matches()

    def _refresh_upcoming_banner_from_data(self, row):
        """Populate upcoming-match banner from pre-fetched row (or None)."""
        if not row:
            self.upcoming_label.setVisible(False)
            return
        date = str(row.get("tourney_date") or "")
        if len(date) == 8:
            date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        opp = row.get("opponent") or "?"
        opp_rank = row.get("opponent_rank")
        opp_str = f"{opp} (#{int(opp_rank)})" if opp_rank else opp
        tourney = row.get("tourney_name") or ""
        round_ = row.get("round") or ""
        round_str = f" \u2013 {round_}" if round_ else ""
        self.upcoming_label.setText(
            f"Next match: {opp_str}  \u00b7  {tourney}{round_str}  \u00b7  {date}"
        )
        self.upcoming_label.setVisible(True)

    def _refresh_upcoming_banner(self):
        """Show next scheduled match for the selected player, if any."""
        if not self._current_player:
            self.upcoming_label.setVisible(False)
            return
        try:
            row = self.db.get_player_upcoming_match(self._current_player)
        except Exception:
            self.upcoming_label.setVisible(False)
            return
        if not row:
            self.upcoming_label.setVisible(False)
            return
        date = str(row.get("tourney_date") or "")
        if len(date) == 8:
            date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        opp = row.get("opponent") or "?"
        opp_rank = row.get("opponent_rank")
        opp_str = f"{opp} (#{int(opp_rank)})" if opp_rank else opp
        tourney = row.get("tourney_name") or ""
        round_ = row.get("round") or ""
        round_str = f" \u2013 {round_}" if round_ else ""
        self.upcoming_label.setText(
            f"Next match: {opp_str}  \u00b7  {tourney}{round_str}  \u00b7  {date}"
        )
        self.upcoming_label.setVisible(True)

    def _connect_filters(self):
        """Wire filter widgets to re-fetch matches when the user changes them."""
        self.surface_pills.changed.connect(lambda _: self._filter_timer.start())
        self.level_pills.changed.connect(lambda _: self._filter_timer.start())
        self.year_combo.currentIndexChanged.connect(
            lambda _: self._filter_timer.start())
        self.round_pills.changed.connect(lambda _: self._filter_timer.start())
        self.rank_pills.changed.connect(lambda _: self._display_page())

    def _refetch_matches(self):
        """Re-query the DB using current filter values for the selected player."""
        if not self._current_player_id:
            return

        surface = self.surface_pills.value()
        surface = None if surface == "All" else surface
        year = self.year_combo.currentText()
        year = None if year == "All" else int(year)
        rounds = self.round_pills.values() or None

        _level_map = {
            "GS": ["G"], "M": ["M", "PM"],
            "ATP": ["A", "P", "I"], "ATP/WTA": ["A", "P", "I"],
            "CH": ["C"], "DC": ["D"], "DC/BJKC": ["D"], "Fin": ["F", "E"],
            "Oly": ["O"],
        }
        selected_pills = self.level_pills.values()
        oly_selected = "Oly" in selected_pills
        atp_selected = "ATP" in selected_pills or "ATP/WTA" in selected_pills
        _tl: list[str] = []
        for v in selected_pills:
            for code in _level_map.get(v, []):
                if code not in _tl:
                    _tl.append(code)
        # Historical Olympics are coded 'A'; add 'A' when Oly is selected
        # without ATP (which already includes 'A')
        filter_hist_oly = oly_selected and not atp_selected
        if filter_hist_oly and "A" not in _tl:
            _tl.append("A")
        tourney_level = _tl or None

        # Stop any previous query
        if self._matches_worker and self._matches_worker.isRunning():
            self._matches_worker.quit()
            self._matches_worker.wait(1000)

        self.status_label.setText("Loading...")

        worker = _MatchesWorker(
            self.db,
            self._current_player_id,
            self._current_player,
            self._current_player_tour,
            surface, tourney_level, year, rounds,
            filter_historical_olympics=filter_hist_oly,
            parent=self,
        )
        worker.data_ready.connect(self._on_matches_loaded)
        worker.error.connect(
            lambda msg: self.status_label.setText(f"Error: {msg}"))
        self._matches_worker = worker
        worker.start()

    def _on_matches_loaded(self, matches, upcoming):
        """Called from worker thread via signal when data is ready."""
        self._all_matches = matches
        self._current_page = 1
        self._refresh_upcoming_banner_from_data(upcoming)
        self._display_page()

    def _get_filtered_matches(self):
        """Return _all_matches filtered by the opponent-rank pill."""
        rank_val = self.rank_pills.value()
        if rank_val == "All":
            return self._all_matches
        limit = int(rank_val.split()[-1])  # "Top 10" → 10
        _pid = str(self._current_player_id or "")
        _pname = (self._current_player or "").replace("-", " ").strip().lower()
        result = []
        for m in self._all_matches:
            wid = str(m.get("winner_id") or "")
            if _pid and wid:
                is_win = (wid == _pid)
            else:
                wname = (m.get("winner_name") or "").replace("-", " ").strip().lower()
                is_win = (wname == _pname)
            opp_rank = m.get("loser_rank") if is_win else m.get("winner_rank")
            try:
                if opp_rank is not None and int(float(opp_rank)) <= limit:
                    result.append(m)
            except (ValueError, TypeError):
                pass
        return result

    def _rows_per_page(self):
        text = self.rows_combo.currentText()
        return None if text == "All" else int(text)

    def _total_pages(self):
        rpp = self._rows_per_page()
        filtered = self._get_filtered_matches()
        if not rpp or not filtered:
            return 1
        return max(1, (len(filtered) + rpp - 1) // rpp)

    def _display_page(self):
        filtered = self._get_filtered_matches()

        # --- W-L record label ---
        _pid = str(self._current_player_id or "")
        _pname = (self._current_player or "").replace("-", " ").strip().lower()
        wins = losses = 0
        for m in filtered:
            wid = str(m.get("winner_id") or "")
            if _pid and wid:
                is_win = wid == _pid
            else:
                wname = (m.get("winner_name") or "").replace("-", " ").strip().lower()
                is_win = wname == _pname
            if is_win:
                wins += 1
            else:
                losses += 1
        if filtered:
            self.wl_label.setText(f"  {wins}W – {losses}L")
        else:
            self.wl_label.setText("")

        if not filtered:
            self.table.setRowCount(0)
            self.status_label.setText("No matches found")
            self.page_label.setText("")
            return

        rpp = self._rows_per_page()
        total_pages = self._total_pages()
        self._current_page = max(1, min(self._current_page, total_pages))

        if rpp:
            start = (self._current_page - 1) * rpp
            page = filtered[start:start + rpp]
        else:
            page = filtered

        level_map = {
            "G": "Grand Slam",
            "M": "Masters", "PM": "W-Premier M",
            "A": "ATP", "P": "W-Premier", "I": "W-Intl",
            "F": "Finals", "E": "W-Elite",
            "D": "Davis Cup", "C": "Challenger", "O": "Olympics",
        }

        rows = []
        for m in page:
            date = str(m.get("tourney_date", ""))
            if len(date) == 8:
                date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
            level = level_map.get(
                m.get("tourney_level", ""), m.get("tourney_level", ""))
            w_rank = str(int(m["winner_rank"])) if m.get("winner_rank") else ""
            l_rank = str(int(m["loser_rank"])) if m.get("loser_rank") else ""
            rows.append([
                date,
                m.get("tourney_name", ""),
                level,
                m.get("surface", ""),
                m.get("round", ""),
                self._format_player(m, "winner"),
                self._format_player(m, "loser"),
                w_rank,
                l_rank,
                m.get("score", ""),
                str(int(m["minutes"])) if m.get("minutes") else "",
            ])

        self.table.populate(rows)

        # Colour the Score cell: bright green = win, bright red = loss
        _pid = str(self._current_player_id or "")
        _pname = (self._current_player or "").replace("-", " ").strip().lower()
        _green = QBrush(QColor("#1a7a3c"))
        _red   = QBrush(QColor("#7a1a1a"))
        _score_col = 9
        for r, m in enumerate(page):
            wid = str(m.get("winner_id") or "")
            wname = (m.get("winner_name") or "").replace("-", " ").strip().lower()
            if wid and _pid:
                is_win = wid == _pid
            elif _pname:
                is_win = wname == _pname
            else:
                continue
            item = self.table.item(r, _score_col)
            if item:
                item.setBackground(_green if is_win else _red)

        page_text = (f"Page {self._current_page} of {total_pages}"
                     if rpp else "All")
        self.page_label.setText(page_text)
        self.status_label.setText(
            f"Showing {len(page)} of {len(filtered)} matches "
            f"for {self._current_player}")

    def _prev_page(self):
        if self._current_page > 1:
            self._current_page -= 1
            self._display_page()

    def _next_page(self):
        if self._current_page < self._total_pages():
            self._current_page += 1
            self._display_page()

    def _on_match_double_clicked(self, index):
        row = index.row()
        # Map visible row → absolute index in filtered matches
        filtered = self._get_filtered_matches()
        rpp = self._rows_per_page()
        if rpp:
            start = (self._current_page - 1) * rpp
            abs_row = start + row
        else:
            abs_row = row
        if 0 <= abs_row < len(filtered):
            match = filtered[abs_row]
            # Column 1 = Tournament → navigate to Tournaments tab
            if index.column() == 1:
                tourney_name = match.get("tourney_name", "")
                date_str = str(match.get("tourney_date", ""))
                year_str = date_str[:4] if len(date_str) >= 4 else ""
                tour = match.get("tour", "")
                self.navigate_to_tournament.emit(tourney_name, year_str, tour)
                return
            dlg = MatchDetailDialog(self.db, match, parent=self)
            dlg.exec()

    @staticmethod
    def _format_player(match, role):
        name = match.get(f"{role}_name", "")
        seed = match.get(f"{role}_seed", "")
        entry = match.get(f"{role}_entry", "")
        ioc = match.get(f"{role}_ioc", "")

        parts = [name]
        tag = ""
        if seed and str(seed).strip():
            try:
                tag = str(int(float(seed)))
            except (ValueError, TypeError):
                tag = str(seed)
        if entry and str(entry).strip():
            tag = f"{tag}/{entry}" if tag else str(entry)
        if tag:
            parts.append(f"({tag})")
        if ioc and str(ioc).strip():
            parts.append(str(ioc))
        return " ".join(parts)
