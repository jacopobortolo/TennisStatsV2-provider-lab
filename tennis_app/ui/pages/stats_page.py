"""
Advanced Statistics page.
"""

import threading
import logging

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWebEngineWidgets import QWebEngineView

from ..widgets import (
    ScrollablePage, PlayerSearchEdit, StatGrid, Separator,
    SectionHeader, PillButtonGroup, MultiPillButtonGroup,
)
from ..charts import spider_chart, pressure_chart, line_chart_trends, get_chart_base_url
from ..theme import COLORS
from ...core.stats_engine import (
    compute_match_stats, aggregate_stats,
    compute_pressure_stats, compute_player_advanced_stats,
    compute_yearly_advanced_stats, filter_matches,
)

logger = logging.getLogger(__name__)


class _ScrapeWorker(QThread):
    """Background thread for scraping extended stats."""
    progress = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, name, db, tour, parent=None):
        super().__init__(parent)
        self._name = name
        self._db = db
        self._tour = tour

    def run(self):
        try:
            from ...core.data_manager import scrape_player_extended_stats
            result = scrape_player_extended_stats(
                self._name, db=self._db, tour=self._tour,
                progress_callback=lambda c, t, m: self.progress.emit(m),
            )
            total = sum(result.values()) if result else 0
            if total:
                self.finished.emit(True,
                                   f"Scraped {total} extended stats for {self._name}")
            else:
                self.finished.emit(True,
                                   f"Extended stats already up-to-date for {self._name}")
        except Exception as exc:
            logger.exception("Extended stats scrape failed")
            self.finished.emit(False, str(exc))


class _LoadPlayerWorker(QThread):
    """Fetch all matches for a player off the UI thread."""
    data_ready = Signal(list, list)  # (matches, years)
    error = Signal(str)

    def __init__(self, db, player, parent=None):
        super().__init__(parent)
        self._db = db
        self._player = player

    def run(self):
        try:
            player_id = self._player["player_id"]
            tour = self._player.get("tour", "atp")
            matches = self._db.get_player_matches(player_id, tour=tour)
            years = sorted(
                {str(m.get("tourney_date", ""))[:4]
                 for m in matches if m.get("tourney_date")},
                reverse=True,
            )
            self.data_ready.emit(matches, years)
        except Exception as exc:
            self.error.emit(str(exc))


class _StatsComputeWorker(QThread):
    """Compute stats + chart HTML off the UI thread."""
    data_ready = Signal(dict)
    error = Signal(str)

    def __init__(self, db, player, matches, filters, parent=None):
        super().__init__(parent)
        self._db = db
        self._player = player
        self._matches = matches
        self._filters = filters

    def run(self):
        try:
            from ...core.scraper import clean_player_name
            player = self._player
            player_id = player["player_id"]
            name = f"{player['name_first']} {player['name_last']}"
            filters = self._filters

            filtered = filter_matches(
                self._matches,
                surface=filters["surface"],
                year=filters["year"],
                tourney_level=filters["tourney_level"],
                round_=filters.get("round_"),
            )
            # Historical Olympics post-filter: 'A' was added to catch pre-modern
            # Olympics data (tourney_level='A', tourney_name contains 'lympic').
            # Remove non-Olympic 'A'-level matches when the user chose Olympics
            # without also selecting ATP/WTA 500/250 events.
            if filters.get("filter_hist_oly"):
                filtered = [
                    m for m in filtered
                    if m.get("tourney_level") != "A"
                    or "lympic" in (m.get("tourney_name") or "")
                ]

            if not filtered:
                self.data_ready.emit({"no_matches": True})
                return

            per_match = []
            for m in filtered:
                ms = compute_match_stats(m, player_id, player_name=name)
                if ms:
                    per_match.append(ms)

            agg = aggregate_stats(per_match)
            pressure = compute_pressure_stats(filtered, player_id,
                                              player_name=name)
            ext_counts = self._db.get_extended_stats_count(
                clean_player_name(name) or name)
            yearly = compute_yearly_advanced_stats(
                self._matches, player_id, player_name=name)

            # --- Build all chart HTML in the background ---
            # Spider chart
            html_spider = ""
            spider_cats, spider_vals = [], []
            for lbl, key in [
                ("Win %", "win_pct"),
                ("1st Serve %", "avg_first_serve_pct"),
                ("1st Serve Won", "avg_first_serve_won_pct"),
                ("2nd Serve Won", "avg_second_serve_won_pct"),
                ("Return Pts Won", "avg_return_pts_won_pct"),
                ("BP Save %", "overall_bp_save_pct"),
            ]:
                val = agg.get(key)
                if val is not None:
                    spider_cats.append(lbl)
                    spider_vals.append(float(val))
            if len(spider_cats) >= 3:
                html_spider = spider_chart(spider_cats, spider_vals, name)

            # Pressure chart
            html_pressure = ""
            if any(pressure.get(k) is not None for k in (
                    "dominance_ratio", "breakpoints_prevail",
                    "deciding_set_win_pct", "tiebreak_win_pct")):
                html_pressure = pressure_chart(pressure) or ""

            # Yearly trend chart
            html_yearly = ""
            if yearly:
                html_yearly = line_chart_trends(yearly, player_id) or ""

            # Extended stats DB queries — use the normalized name so it
            # matches the storage name written by the scraper (which also
            # normalizes via clean_player_name before writing to the DB).
            # NOTE: tourney_level and round_ are NOT passed here because
            # those columns are not reliably populated in the extended stats
            # tables (many rows have NULL).  Only surface and year are
            # applied; tourney_level / round filtering is handled above via
            # filter_matches on the main matches table.
            db_name = clean_player_name(name) or name
            fkw = dict(surface=filters["surface"], year=filters["year"])
            ext_data = {}
            if ext_counts.get("match_winners_errors", 0) > 0:
                ext_data["winners_errors"] = self._db.get_player_winners_errors(db_name, **fkw)
            if ext_counts.get("match_serve_speed", 0) > 0:
                ext_data["serve_speed"] = self._db.get_player_serve_speed(db_name, **fkw)
            if ext_counts.get("match_pbp_stats", 0) > 0:
                ext_data["pbp_stats"] = self._db.get_player_pbp_stats(db_name, **fkw)
            if ext_counts.get("match_mcp_serve", 0) > 0:
                ext_data["mcp_serve"] = self._db.get_player_mcp_serve(db_name, **fkw)
            if ext_counts.get("match_mcp_tactics", 0) > 0:
                ext_data["mcp_tactics"] = self._db.get_player_mcp_tactics(db_name, **fkw)

            # Filter summary text
            filter_text = []
            if filters["surface"]:
                filter_text.append(filters["surface"])
            if filters["year"]:
                filter_text.append(filters["year"])
            if filters["tourney_level"]:
                lv = filters["tourney_level"]
                if isinstance(lv, (list, tuple, set)):
                    lv = "+".join(sorted(lv))
                filter_text.append(f"Level: {lv}")
            if filters.get("round_"):
                rd = filters["round_"]
                if isinstance(rd, (list, tuple, set)):
                    rd = "+".join(sorted(rd))
                filter_text.append(f"Round: {rd}")
            filter_str = " | ".join(filter_text) if filter_text else "All matches"

            self.data_ready.emit({
                "no_matches": False,
                "name": name,
                "agg": agg,
                "pressure": pressure,
                "ext_counts": ext_counts,
                "yearly": yearly,
                "html_spider": html_spider,
                "html_pressure": html_pressure,
                "html_yearly": html_yearly,
                "ext_data": ext_data,
                "filter_str": filter_str,
                "match_count": len(filtered),
                "filters": filters,
            })
        except Exception as exc:
            logger.exception("Stats computation failed")
            self.error.emit(str(exc))


class StatsPage(QWidget):
    """Advanced player statistics dashboard."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_player = None
        self._cached_matches = []
        self._load_worker = None
        self._stats_worker = None
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(250)
        self._filter_timer.timeout.connect(self._refresh_stats)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Top bar: search + filters ---
        top_bar = QWidget()
        top_bar.setObjectName("statsTopBar")
        top_bar.setStyleSheet(
            f"QWidget#statsTopBar {{ background-color: {COLORS['bg_secondary']}; }}")
        tb_layout = QVBoxLayout(top_bar)
        tb_layout.setContentsMargins(24, 12, 24, 12)
        tb_layout.setSpacing(8)

        # Search row
        search_row = QHBoxLayout()
        self.player_edit = PlayerSearchEdit(self.db, placeholder="Player name...")
        self.player_edit.player_selected.connect(self._on_player_selected)
        search_row.addWidget(self.player_edit)

        load_btn = QPushButton("📊 Load Stats")
        load_btn.setObjectName("accentBtn")
        load_btn.setCursor(Qt.PointingHandCursor)
        load_btn.clicked.connect(self._on_load_clicked)
        search_row.addWidget(load_btn)

        self.scrape_btn = QPushButton("🌐 Fetch Extended Stats")
        self.scrape_btn.setObjectName("accentBtn")
        self.scrape_btn.setCursor(Qt.PointingHandCursor)
        self.scrape_btn.clicked.connect(self._on_scrape_extended)
        search_row.addWidget(self.scrape_btn)

        self.status_label = QLabel("")
        self.status_label.setObjectName("dimLabel")
        self.status_label.setStyleSheet("background: transparent;")
        search_row.addWidget(self.status_label)

        tb_layout.addLayout(search_row)

        # Filter row — pill buttons
        filter_row = QHBoxLayout()
        filter_row.setSpacing(16)

        surf_label = QLabel("Surface:")
        surf_label.setObjectName("dimLabel")
        surf_label.setStyleSheet("background: transparent;")
        filter_row.addWidget(surf_label)
        self.surface_pills = PillButtonGroup(
            ["All", "Hard", "Clay", "Grass", "Carpet"])
        self.surface_pills.changed.connect(lambda _: self._filter_timer.start())
        filter_row.addWidget(self.surface_pills)

        filter_row.addWidget(QLabel("  "))  # spacer

        year_label = QLabel("Year:")
        year_label.setObjectName("dimLabel")
        year_label.setStyleSheet("background: transparent;")
        filter_row.addWidget(year_label)
        self.year_combo = QComboBox()
        self.year_combo.addItems(["All"])
        self.year_combo.currentIndexChanged.connect(
            lambda _: self._filter_timer.start())
        filter_row.addWidget(self.year_combo)

        level_label = QLabel("Level:")
        level_label.setObjectName("dimLabel")
        level_label.setStyleSheet("background: transparent;")
        filter_row.addWidget(level_label)
        self.level_pills = MultiPillButtonGroup(
            ["All", "G", "M", "A", "F", "D", "O"])
        self.level_pills.changed.connect(lambda _: self._filter_timer.start())
        filter_row.addWidget(self.level_pills)

        round_label = QLabel("Round:")
        round_label.setObjectName("dimLabel")
        round_label.setStyleSheet("background: transparent;")
        filter_row.addWidget(round_label)
        self.round_pills = MultiPillButtonGroup(
            ["All", "F", "SF", "QF", "R16", "R32", "R64", "R128", "RR"])
        self.round_pills.changed.connect(lambda _: self._filter_timer.start())
        filter_row.addWidget(self.round_pills)

        filter_row.addStretch()
        tb_layout.addLayout(filter_row)
        layout.addWidget(top_bar)

        # --- Content area ---
        self.content = ScrollablePage()
        layout.addWidget(self.content, 1)

        placeholder = QLabel("Search for a player to view advanced statistics.")
        placeholder.setObjectName("dimLabel")
        placeholder.setAlignment(Qt.AlignCenter)
        self.content.content_layout.addWidget(placeholder)

    def _get_filters(self):
        surface = self.surface_pills.value()
        year = self.year_combo.currentText()
        _wta_extra = {"M": ["PM"], "A": ["P", "I"], "F": ["E"], "O": []}
        raw_levels = self.level_pills.values()
        filter_hist_oly = False
        if raw_levels:
            expanded: list[str] = []
            for lv in raw_levels:
                if lv not in expanded:
                    expanded.append(lv)
                for extra in _wta_extra.get(lv, []):
                    if extra not in expanded:
                        expanded.append(extra)
            # Historical Olympics: pre-modern data uses tourney_level='A' with
            # tourney_name containing 'lympic'. When 'O' is selected but 'A' is
            # not (user didn't explicitly select regular ATP/WTA 500/250 events),
            # add 'A' and flag for post-filtering.
            if "O" in expanded and "A" not in expanded:
                expanded.append("A")
                filter_hist_oly = True
            levels = expanded
        else:
            levels = None
        rounds = self.round_pills.values() or None
        return {
            "surface": surface if surface != "All" else None,
            "year": year if year != "All" else None,
            "tourney_level": levels,
            "round_": rounds,
            "filter_hist_oly": filter_hist_oly,
        }

    def _on_player_selected(self, player):
        """Called when a player is picked from the dropdown."""
        self._load_player(player)

    def _on_load_clicked(self):
        """Called when the Load Stats button is clicked."""
        player = self.player_edit.selected_player()
        if not player:
            query = self.player_edit.text()
            if len(query) >= 2:
                results = self.db.search_players(query, limit=1)
                player = results[0] if results else None
        if not player:
            self.status_label.setText("Player not found")
            return
        self._load_player(player)

    def _load_player(self, player):
        self.current_player = player
        self.status_label.setText("Loading matches…")

        # Show spinner in content area
        self.content.begin_update()
        lbl = QLabel("Loading player data…")
        lbl.setObjectName("dimLabel")
        lbl.setAlignment(Qt.AlignCenter)
        self.content.content_layout.addWidget(lbl)
        self.content.end_update()

        # Stop any running workers
        for w in (self._load_worker, self._stats_worker):
            if w and w.isRunning():
                w.quit()
                w.wait(2000)

        worker = _LoadPlayerWorker(self.db, player, parent=self)
        worker.data_ready.connect(self._on_matches_loaded)
        worker.error.connect(lambda msg: self.status_label.setText(f"Error: {msg}"))
        self._load_worker = worker
        worker.start()

    def _on_matches_loaded(self, matches, years):
        self.year_combo.blockSignals(True)
        self.year_combo.clear()
        self.year_combo.addItems(["All"] + years)
        self.year_combo.blockSignals(False)
        self._cached_matches = matches
        self._refresh_stats()

    def _refresh_stats(self):
        if not self.current_player:
            return

        # Stop any previous stats computation
        if self._stats_worker and self._stats_worker.isRunning():
            self._stats_worker.quit()
            self._stats_worker.wait(2000)

        filters = self._get_filters()
        worker = _StatsComputeWorker(
            self.db, self.current_player, self._cached_matches, filters, parent=self)
        worker.data_ready.connect(self._on_stats_data)
        worker.error.connect(lambda msg: self.status_label.setText(f"Error: {msg}"))
        self._stats_worker = worker
        worker.start()

    def _on_stats_data(self, payload):
        if payload.get("no_matches"):
            self.content.begin_update()
            lbl = QLabel("No matches found with current filters")
            lbl.setObjectName("dimLabel")
            lbl.setAlignment(Qt.AlignCenter)
            self.content.content_layout.addWidget(lbl)
            self.content.end_update()
            self.status_label.setText("No matches")
            return

        name = payload["name"]
        agg = payload["agg"]
        pressure = payload["pressure"]
        ext_counts = payload["ext_counts"]
        ext_data = payload["ext_data"]
        html_spider = payload["html_spider"]
        html_pressure = payload["html_pressure"]
        html_yearly = payload["html_yearly"]
        filter_str = payload["filter_str"]
        match_count = payload["match_count"]
        filters = payload["filters"]

        self.content.begin_update()
        self._display_stats_content(
            name, agg, pressure, ext_counts, ext_data,
            html_spider, html_pressure, html_yearly,
            filter_str, match_count, filters)
        self.content.end_update()

    def _display_stats(self, name, agg, pressure, ext_counts, match_count):
        self.content.begin_update()
        self._display_stats_content(name, agg, pressure, ext_counts, {}, "", "", "",
                                    "All matches", match_count, {})
        self.content.end_update()

    def _display_stats_content(self, name, agg, pressure, ext_counts, ext_data,
                                html_spider, html_pressure, html_yearly,
                                filter_str, match_count, filters):
        layout = self.content.content_layout

        # Header
        title = QLabel(f"📊 Advanced Stats: {name}")
        title.setObjectName("titleLabel")
        layout.addWidget(title)

        subtitle = QLabel(f"Based on {match_count} matches — {filter_str}")
        subtitle.setObjectName("dimLabel")
        layout.addWidget(subtitle)

        self.status_label.setText(f"{match_count} matches loaded")

        layout.addWidget(Separator())

        # --- Spider chart (HTML pre-built in worker) ---
        if html_spider:
            chart = QWebEngineView()
            chart.page().setBackgroundColor(QColor(COLORS["bg_primary"]))
            chart.setFixedHeight(380)
            chart.setHtml(html_spider, get_chart_base_url())
            layout.addWidget(chart)
            layout.addWidget(Separator())

        # --- Overview ---
        self._section_header(layout, "Overview")
        grid = StatGrid(columns=4)
        grid.add_stat("W-L", f"{agg.get('wins', 0)}-{agg.get('losses', 0)}",
                       icon="🎾")
        grid.add_stat("Win %", f"{agg.get('win_pct', 0)}%", icon="📈")
        if agg.get("straight_set_wins") is not None:
            grid.add_stat(
                "Straight Set Wins",
                f"{agg['straight_set_wins']} ({agg.get('straight_set_win_pct', 0)}%)")
        if agg.get("deciding_set_wins") is not None:
            grid.add_stat(
                "Deciding Sets",
                f"{agg['deciding_set_wins']}-{agg.get('deciding_set_losses', 0)} "
                f"({agg.get('deciding_set_pct', 0)}%)")
        if agg.get("sets_won") is not None:
            grid.add_stat("Sets W-L",
                          f"{agg['sets_won']}-{agg.get('sets_lost', 0)}")
        if agg.get("games_won") is not None:
            grid.add_stat("Games W-L",
                          f"{agg['games_won']}-{agg.get('games_lost', 0)}")
        if agg.get("tiebreak_pct") is not None:
            grid.add_stat(
                "Tiebreaks",
                f"{agg.get('tiebreaks_won', 0)}-{agg.get('tiebreaks_lost', 0)} "
                f"({agg['tiebreak_pct']}%)")
        if agg.get("avg_match_duration") is not None:
            grid.add_stat("Avg Duration",
                          f"{int(agg['avg_match_duration'])} min")
        layout.addWidget(grid)
        layout.addWidget(Separator())

        # --- Tournament Progression ---
        if agg.get("rounds_played"):
            self._section_header(layout, "Tournament Progression")
            prog_grid = StatGrid(columns=4)
            rp = agg["rounds_played"]
            rw = agg.get("rounds_won", {})

            def _pct(w, t):
                return f" ({round(w/t*100)}%)" if t else ""

            if agg.get("titles") is not None:
                finals = agg.get("finals", 0)
                titles = agg["titles"]
                prog_grid.add_stat(
                    "Titles", f"{titles}{_pct(titles, finals)}",
                    icon="🏆")
            if agg.get("finals", 0) > 0:
                f_w = rw.get("F", 0)
                f_p = rp.get("F", 0)
                prog_grid.add_stat(
                    "Finals", f"{f_w}W – {f_p - f_w}L",
                    icon="🥈")
            if agg.get("semis", 0) > 0:
                sf_w = rw.get("SF", 0)
                sf_p = rp.get("SF", 0)
                prog_grid.add_stat(
                    "Semi-Finals", f"{sf_w}W – {sf_p - sf_w}L",
                    icon="4️⃣")
            if agg.get("quarters", 0) > 0:
                qf_w = rw.get("QF", 0)
                qf_p = rp.get("QF", 0)
                prog_grid.add_stat(
                    "Quarter-Finals", f"{qf_w}W – {qf_p - qf_w}L")
            if agg.get("r16", 0) > 0:
                r16_w = rw.get("R16", 0)
                r16_p = rp.get("R16", 0)
                prog_grid.add_stat(
                    "Round of 16", f"{r16_w}W – {r16_p - r16_w}L")
            if agg.get("r32", 0) > 0:
                r32_w = rw.get("R32", 0)
                r32_p = rp.get("R32", 0)
                prog_grid.add_stat(
                    "Round of 32", f"{r32_w}W – {r32_p - r32_w}L")
            if agg.get("r64", 0) > 0:
                r64_w = rw.get("R64", 0)
                r64_p = rp.get("R64", 0)
                prog_grid.add_stat(
                    "Round of 64", f"{r64_w}W – {r64_p - r64_w}L")
            # RR (round robin) if present
            rr_p = rp.get("RR", 0)
            if rr_p > 0:
                rr_w = rw.get("RR", 0)
                prog_grid.add_stat(
                    "Round Robin", f"{rr_w}W – {rr_p - rr_w}L")
            layout.addWidget(prog_grid)
            layout.addWidget(Separator())

        # --- Serve ---
        self._section_header(layout, "Serve")
        serve_grid = StatGrid(columns=4)
        for label, key in [
            ("Aces/Match", "aces_per_match"),
            ("DFs/Match", "dfs_per_match"),
            ("1st Serve %", "avg_first_serve_pct"),
            ("1st Serve Won", "avg_first_serve_won_pct"),
            ("2nd Serve Won", "avg_second_serve_won_pct"),
            ("Total SvPts Won", "avg_total_serve_won_pct"),
            ("Hold %", "overall_hold_pct"),
        ]:
            val = agg.get(key)
            if val is not None:
                suffix = "%" if "pct" in key.lower() or "won" in key.lower() else ""
                serve_grid.add_stat(label, f"{val}{suffix}")
        layout.addWidget(serve_grid)
        layout.addWidget(Separator())

        # --- Return ---
        self._section_header(layout, "Return")
        ret_grid = StatGrid(columns=4)
        for label, key in [
            ("Return Pts Won", "avg_return_pts_won_pct"),
            ("1st Return Won", "avg_first_return_won_pct"),
            ("2nd Return Won", "avg_second_return_won_pct"),
            ("BP Conversion", "overall_bp_conversion_pct"),
        ]:
            val = agg.get(key)
            if val is not None:
                ret_grid.add_stat(label, f"{val}%")
        layout.addWidget(ret_grid)
        layout.addWidget(Separator())

        # --- Pressure ---
        self._section_header(layout, "Pressure & Clutch")
        if html_pressure:
            chart = QWebEngineView()
            chart.page().setBackgroundColor(QColor(COLORS["bg_primary"]))
            chart.setFixedHeight(300)
            chart.setHtml(html_pressure, get_chart_base_url())
            layout.addWidget(chart)

        press_grid = StatGrid(columns=4)
        if agg.get("overall_bp_save_pct") is not None:
            press_grid.add_stat("BP Save %",
                                f"{agg['overall_bp_save_pct']}%")
        if pressure.get("dominance_ratio") is not None:
            press_grid.add_stat("Dominance Ratio",
                                str(pressure["dominance_ratio"]))
        if pressure.get("breakpoints_prevail") is not None:
            press_grid.add_stat("BP Prevail",
                                str(pressure["breakpoints_prevail"]))
        if agg.get("avg_total_points_won_pct") is not None:
            press_grid.add_stat("Total Points Won",
                                f"{agg['avg_total_points_won_pct']}%")
        layout.addWidget(press_grid)
        layout.addWidget(Separator())

        # --- Extended stats ---
        total_ext = sum(ext_counts.values())
        self._section_header(
            layout, f"Extended Stats (scraped: {total_ext} records)")
        if total_ext == 0:
            lbl = QLabel(
                'No extended stats available. '
                'Click "Fetch Extended Stats" to scrape.')
            lbl.setObjectName("dimLabel")
            layout.addWidget(lbl)
        else:
            self._display_extended_stats(layout, ext_counts, ext_data)

        layout.addWidget(Separator())

        # --- Yearly trend ---
        self._section_header(layout, "Yearly Trend")
        if html_yearly:
            chart = QWebEngineView()
            chart.page().setBackgroundColor(QColor(COLORS["bg_primary"]))
            chart.setFixedHeight(300)
            chart.setHtml(html_yearly, get_chart_base_url())
            layout.addWidget(chart)
        else:
            lbl = QLabel("Not enough data for yearly trend.")
            lbl.setObjectName("dimLabel")
            layout.addWidget(lbl)

    def _display_extended_stats(self, layout, ext_counts, ext_data):
        grid = StatGrid(columns=4)

        # Winners/Errors
        we_data = ext_data.get("winners_errors")
        if we_data:
            w_total = sum(r.get("winners") or 0 for r in we_data)
            ue_total = sum(r.get("unforced_errors") or 0 for r in we_data)
            n = len(we_data)
            grid.add_stat("Winners/Match", f"{w_total / n:.1f}")
            grid.add_stat("UE/Match", f"{ue_total / n:.1f}")
            if ue_total > 0:
                grid.add_stat("W/UE Ratio", f"{w_total / ue_total:.2f}")
            grid.add_stat("W/E Coverage", f"{n} matches")

        # Serve Speed
        speed_data = ext_data.get("serve_speed")
        if speed_data:
            avg_1st = [r["first_serve_avg"] for r in speed_data if r.get("first_serve_avg")]
            max_1st = [r["first_serve_max"] for r in speed_data if r.get("first_serve_max")]
            avg_2nd = [r["second_serve_avg"] for r in speed_data if r.get("second_serve_avg")]
            if avg_1st:
                grid.add_stat("1st Serve Avg",
                              f"{sum(avg_1st) / len(avg_1st) * 1.60934:.1f} km/h")
            if max_1st:
                grid.add_stat("1st Serve Max",
                              f"{max(max_1st) * 1.60934:.0f} km/h")
            if avg_2nd:
                grid.add_stat("2nd Serve Avg",
                              f"{sum(avg_2nd) / len(avg_2nd) * 1.60934:.1f} km/h")

        # PBP Stats
        pbp_data = ext_data.get("pbp_stats")
        if pbp_data:
            rally_lens = [r["rally_length_avg"] for r in pbp_data if r.get("rally_length_avg")]
            agg_margins = [r["aggressive_margin"] for r in pbp_data
                           if r.get("aggressive_margin") is not None]
            if rally_lens:
                grid.add_stat("Rally Length",
                              f"{sum(rally_lens) / len(rally_lens):.1f}")
            if agg_margins:
                grid.add_stat("Aggr. Margin",
                              f"{sum(agg_margins) / len(agg_margins):.1f}")

        # MCP Serve Direction
        srv_data = ext_data.get("mcp_serve")
        if srv_data:
            ace_pcts = [r["ace_pct"] for r in srv_data if r.get("ace_pct") is not None]
            if ace_pcts:
                grid.add_stat("Ace %", f"{sum(ace_pcts) / len(ace_pcts):.1f}%")

        # MCP Tactics
        tac_data = ext_data.get("mcp_tactics")
        if tac_data:
            net_appr = [r["net_approach_pct"] for r in tac_data
                        if r.get("net_approach_pct") is not None]
            if net_appr:
                grid.add_stat("Net Approach %",
                              f"{sum(net_appr) / len(net_appr):.1f}%")

        layout.addWidget(grid)

    def _section_header(self, layout, text):
        layout.addWidget(SectionHeader(text))

    def _on_scrape_extended(self):
        if not self.current_player:
            self.status_label.setText("Search for a player first")
            return

        player = self.current_player
        from ...core.scraper import clean_player_name
        raw_name = f"{player['name_first']} {player['name_last']}"
        name = clean_player_name(raw_name) or raw_name
        tour = player.get("tour", "atp")
        self.status_label.setText(f"Scraping extended stats for {name}...")
        self.scrape_btn.setEnabled(False)

        self._scrape_worker = _ScrapeWorker(name, self.db, tour, self)
        self._scrape_worker.progress.connect(
            lambda msg: self.status_label.setText(msg))
        self._scrape_worker.finished.connect(self._on_scrape_done)
        self._scrape_worker.start()

    def _on_scrape_done(self, success, msg):
        self.scrape_btn.setEnabled(True)
        if success:
            self.status_label.setText(msg)
            self._refresh_stats()
        else:
            self.status_label.setText(f"Scrape error: {msg}")
