"""
Head-to-Head comparison page.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebEngineWidgets import QWebEngineView

from ..widgets import (
    ScrollablePage, StatGrid, Separator, DataTable,
    SectionHeader, ComparisonBar, PlayerSearchEdit,
)
from ..charts import bar_chart_h2h, spider_chart_dual, get_chart_base_url
from ..theme import COLORS, FONTS
from ..match_detail_dialog import MatchDetailDialog


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class _H2HWorker(QThread):
    """Fetch H2H data and career stats off the UI thread."""

    data_ready = Signal(dict)
    error = Signal(str)

    def __init__(self, db, p1, p2, parent=None):
        super().__init__(parent)
        self._db = db
        self._p1 = p1
        self._p2 = p2

    def run(self):
        try:
            p1, p2 = self._p1, self._p2
            p1_id, p2_id = p1["player_id"], p2["player_id"]
            p1_tour = p1.get("tour")
            p1_name = f"{p1['name_first']} {p1['name_last']}"
            p2_name = f"{p2['name_first']} {p2['name_last']}"

            h2h = self._db.get_head_to_head(p1_id, p2_id, tour=p1_tour)
            if h2h["total_matches"] == 0:
                self.data_ready.emit({
                    "no_matches": True,
                    "p1_name": p1_name,
                    "p2_name": p2_name,
                })
                return

            stats1 = self._db.get_player_career_stats(p1_id, tour=p1_tour)
            stats2 = self._db.get_player_career_stats(p2_id, tour=p1_tour)

            # Build comparisons list
            def _safe_pct(wins, losses):
                total = wins + losses
                return round(wins / total * 100, 1) if total else 0

            comparisons = []
            comparisons.append(("Win %",
                                 _safe_pct(stats1["wins"], stats1["losses"]),
                                 _safe_pct(stats2["wins"], stats2["losses"])))
            if stats1.get("serve") and stats2.get("serve"):
                for key, label in [
                    ("first_serve_pct", "1st Serve %"),
                    ("first_serve_won_pct", "1st Srv Won %"),
                    ("second_serve_won_pct", "2nd Srv Won %"),
                    ("bp_saved_pct", "BP Saved %"),
                ]:
                    sv1 = float(stats1["serve"].get(key, 0))
                    sv2 = float(stats2["serve"].get(key, 0))
                    if sv1 or sv2:
                        comparisons.append((label, sv1, sv2))

            # Pre-build chart HTML
            surfaces_data = h2h.get("by_surface", {})
            html_h2h_bar = bar_chart_h2h(surfaces_data, p1_name, p2_name) if surfaces_data else ""

            categories = [c[0] for c in comparisons]
            vals1 = [c[1] for c in comparisons]
            vals2 = [c[2] for c in comparisons]
            html_spider = (spider_chart_dual(categories, vals1, p1_name, vals2, p2_name)
                           if len(categories) >= 3 else "")

            self.data_ready.emit({
                "no_matches": False,
                "p1_name": p1_name,
                "p2_name": p2_name,
                "h2h": h2h,
                "stats1": stats1,
                "stats2": stats2,
                "comparisons": comparisons,
                "html_h2h_bar": html_h2h_bar,
                "html_spider": html_spider,
            })
        except Exception as exc:
            self.error.emit(str(exc))


class H2HPage(QWidget):
    """Head-to-head comparison between two players."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._p1 = None  # selected player dict
        self._p2 = None
        self._h2h_matches = []  # flat list, filled after _compare
        self._h2h_worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Input bar ---
        input_bar = QWidget()
        input_bar.setObjectName("h2hInputBar")
        input_bar.setStyleSheet(
            f"QWidget#h2hInputBar {{ background-color: {COLORS['bg_secondary']};"
            f" border-bottom: 1px solid {COLORS['border']}; }}")
        ib_layout = QHBoxLayout(input_bar)
        ib_layout.setContentsMargins(28, 14, 28, 14)
        ib_layout.setSpacing(12)

        lbl1 = QLabel("Player 1:")
        lbl1.setStyleSheet(f"color: {COLORS['text_dim']}; background: transparent;")
        ib_layout.addWidget(lbl1)
        self.p1_edit = PlayerSearchEdit(self.db, placeholder="e.g. Sinner")
        self.p1_edit.player_selected.connect(lambda p: setattr(self, '_p1', p))
        ib_layout.addWidget(self.p1_edit)

        vs_label = QLabel("⚔️")
        vs_label.setStyleSheet(
            f"font-size: {FONTS['size_xl']}pt; background: transparent;")
        ib_layout.addWidget(vs_label)

        lbl2 = QLabel("Player 2:")
        lbl2.setStyleSheet(f"color: {COLORS['text_dim']}; background: transparent;")
        ib_layout.addWidget(lbl2)
        self.p2_edit = PlayerSearchEdit(self.db, placeholder="e.g. Alcaraz")
        self.p2_edit.player_selected.connect(lambda p: setattr(self, '_p2', p))
        ib_layout.addWidget(self.p2_edit)

        compare_btn = QPushButton("⚔️ Compare")
        compare_btn.setObjectName("accentBtn")
        compare_btn.setCursor(Qt.PointingHandCursor)
        compare_btn.clicked.connect(self._compare)
        ib_layout.addWidget(compare_btn)

        layout.addWidget(input_bar)

        # --- Result area ---
        self.result_area = ScrollablePage()
        layout.addWidget(self.result_area, 1)

        placeholder = QLabel("Enter two player names and click Compare")
        placeholder.setObjectName("dimLabel")
        placeholder.setAlignment(Qt.AlignCenter)
        self.result_area.content_layout.addWidget(placeholder)

    def _compare(self):
        # Use autocomplete-selected players if available, else search
        p1 = self._p1 or (self.p1_edit.selected_player())
        p2 = self._p2 or (self.p2_edit.selected_player())

        if not p1:
            q1 = self.p1_edit.text()
            if len(q1) >= 2:
                results = self.db.search_players(q1, limit=1)
                p1 = results[0] if results else None
        if not p2:
            q2 = self.p2_edit.text()
            if len(q2) >= 2:
                results = self.db.search_players(q2, limit=1)
                p2 = results[0] if results else None

        if not p1 or not p2:
            self.result_area.clear_content()
            lbl = QLabel("One or both players not found.")
            lbl.setObjectName("dimLabel")
            lbl.setAlignment(Qt.AlignCenter)
            self.result_area.content_layout.addWidget(lbl)
            return

        # Show spinner immediately
        self.result_area.clear_content()
        spinner = QLabel("Loading comparison…")
        spinner.setObjectName("dimLabel")
        spinner.setAlignment(Qt.AlignCenter)
        self.result_area.content_layout.addWidget(spinner)

        # Stop any previous worker
        if self._h2h_worker and self._h2h_worker.isRunning():
            self._h2h_worker.quit()
            self._h2h_worker.wait(2000)

        worker = _H2HWorker(self.db, p1, p2, parent=self)
        worker.data_ready.connect(self._on_h2h_data)
        worker.error.connect(self._on_h2h_error)
        self._h2h_worker = worker
        worker.start()

    def _on_h2h_error(self, msg):
        self.result_area.clear_content()
        lbl = QLabel(f"Error: {msg}")
        lbl.setObjectName("dimLabel")
        lbl.setAlignment(Qt.AlignCenter)
        self.result_area.content_layout.addWidget(lbl)

    def _on_h2h_data(self, payload):
        p1_name = payload["p1_name"]
        p2_name = payload["p2_name"]

        if payload.get("no_matches"):
            self.result_area.clear_content()
            lbl = QLabel(f"No matches found between {p1_name} and {p2_name}")
            lbl.setObjectName("subHeaderLabel")
            lbl.setAlignment(Qt.AlignCenter)
            self.result_area.content_layout.addWidget(lbl)
            return

        h2h = payload["h2h"]
        stats1 = payload["stats1"]
        stats2 = payload["stats2"]
        comparisons = payload["comparisons"]
        html_h2h_bar = payload["html_h2h_bar"]
        html_spider = payload["html_spider"]

        # All data ready — build new content off-screen, then swap
        self.result_area.begin_update()
        layout = self.result_area.content_layout

        # --- Header: big score ---
        header_row = QHBoxLayout()
        header_row.setAlignment(Qt.AlignCenter)
        header_row.setSpacing(20)

        lbl1 = QLabel(p1_name)
        lbl1.setStyleSheet(
            f"font-size: {FONTS['size_xl']}pt; font-weight: 700;"
            f" color: {COLORS['accent']};")
        header_row.addWidget(lbl1)

        score = QLabel(f"  {h2h['p1_wins']}  —  {h2h['p2_wins']}  ")
        score.setStyleSheet(
            f"font-size: {FONTS['size_hero']}pt; font-weight: 800; "
            f"color: {COLORS['text']};")
        header_row.addWidget(score)

        lbl2 = QLabel(p2_name)
        lbl2.setStyleSheet(
            f"font-size: {FONTS['size_xl']}pt; font-weight: 700;"
            f" color: {COLORS['red']};")
        header_row.addWidget(lbl2)

        header_widget = QWidget()
        header_widget.setLayout(header_row)
        layout.addWidget(header_widget)
        layout.addWidget(Separator())

        # --- By surface cards ---
        layout.addWidget(SectionHeader("By Surface"))

        surfaces_data = h2h.get("by_surface", {})
        if surfaces_data:
            row = QHBoxLayout()

            if html_h2h_bar:
                chart = QWebEngineView()
                chart.page().setBackgroundColor(QColor(COLORS["bg_primary"]))
                chart.setFixedHeight(280)
                chart.setHtml(html_h2h_bar, get_chart_base_url())
                row.addWidget(chart, 2)

            # Surface stat cards
            grid = StatGrid(columns=2)
            for surface, rec in surfaces_data.items():
                grid.add_stat(surface, f"{rec['p1_wins']} - {rec['p2_wins']}")
            row.addWidget(grid, 1)

            row_widget = QWidget()
            row_widget.setLayout(row)
            layout.addWidget(row_widget)

        layout.addWidget(Separator())

        # --- Key Stats Comparison (side-by-side bars) ---
        if comparisons:
            layout.addWidget(SectionHeader("Key Stats Comparison"))
            for stat_label, val1, val2 in comparisons:
                bar = ComparisonBar(stat_label, val1, val2, fmt=".1f")
                layout.addWidget(bar)
            layout.addWidget(Separator())

        # --- Spider chart comparison (career stats) ---
        if html_spider:
            layout.addWidget(SectionHeader("Performance Spider Comparison"))
            chart = QWebEngineView()
            chart.page().setBackgroundColor(QColor(COLORS["bg_primary"]))
            chart.setFixedHeight(400)
            chart.setHtml(html_spider, get_chart_base_url())
            layout.addWidget(chart)
            layout.addWidget(Separator())

        # --- Match list ---
        layout.addWidget(SectionHeader("All Matches"))

        match_table = DataTable([
            ("Date", 90), ("Tournament", 180), ("Surface", 70),
            ("Round", 60), ("Winner", 160), ("Score", 180),
        ])
        rows = []
        for m in h2h["matches"]:
            date = str(m.get("tourney_date", ""))
            if len(date) == 8:
                date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
            rows.append([
                date,
                m.get("tourney_name", ""),
                m.get("surface", ""),
                m.get("round", ""),
                m.get("winner_name", ""),
                m.get("score", ""),
            ])
        match_table.populate(rows)
        # Resize the table to show all rows without internal scrollbar
        match_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header_h = match_table.horizontalHeader().height()
        row_h = match_table.rowHeight(0) if match_table.rowCount() > 0 else 30
        match_table.setFixedHeight(
            header_h + row_h * match_table.rowCount() + 2)
        # Store match list and wire double-click
        self._h2h_matches = list(h2h["matches"])
        match_table.doubleClicked.connect(self._on_match_double_clicked)
        layout.addWidget(match_table)

        self.result_area.end_update()

    def _on_match_double_clicked(self, index):
        row = index.row()
        if 0 <= row < len(self._h2h_matches):
            dlg = MatchDetailDialog(self.db, self._h2h_matches[row], parent=self)
            dlg.exec()
