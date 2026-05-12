"""
Rankings browser page.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor

from ..widgets import DataTable, Separator, PillButtonGroup, SectionHeader
from ..theme import COLORS


class _RankingScrapeWorker(QThread):
    """Background thread for scraping rankings so the UI stays responsive."""
    finished = Signal(object, object)  # (rankings_list, date_str)

    def __init__(self, db, tour, discipline, source, top_n, parent=None):
        super().__init__(parent)
        self._db = db
        self._tour = tour
        self._discipline = discipline
        self._source = source
        self._top_n = top_n

    def run(self):
        try:
            source_map = {"LIVE": "LIVE", "RACE": "RACE", "OFFICIAL": "OFFICIAL"}
            marker, _ = self._db.refresh_scraped_rankings(
                tour=self._tour, discipline=self._discipline,
                source=source_map[self._source],
            )
            rankings, date = self._db.get_rankings(
                tour=self._tour, top_n=self._top_n, date=marker)
            self.finished.emit(rankings, date)
        except Exception:
            self.finished.emit(None, None)


class RankingsPage(QWidget):
    """Page for browsing ATP/WTA rankings."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._selected_category = "atp_singles"
        self._selected_source = "OFFICIAL"
        self._date_map = {}
        self._all_rankings = []
        self._scrape_worker = None
        self._first_show = True
        self._build_ui()

    def showEvent(self, event):
        """Defer the first ranking refresh until the page is actually shown."""
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            self._refresh_rankings()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # --- Header ---
        header = QLabel("Rankings")
        header.setObjectName("headerLabel")
        layout.addWidget(header)

        # --- Category pills ---
        cat_row = QHBoxLayout()
        cat_row.setSpacing(8)

        self.cat_pills = PillButtonGroup(
            ["ATP Singles", "ATP Doubles", "WTA Singles", "WTA Doubles"],
            default="ATP Singles")
        self.cat_pills.changed.connect(self._on_cat_pill_change)
        cat_row.addWidget(self.cat_pills)

        cat_row.addStretch()
        self.date_info_label = QLabel("")
        self.date_info_label.setObjectName("dimLabel")
        cat_row.addWidget(self.date_info_label)

        layout.addLayout(cat_row)

        # --- Source pills + date combo + filter ---
        src_row = QHBoxLayout()
        src_row.setSpacing(8)

        self.src_pills = PillButtonGroup(
            ["OFFICIAL", "LIVE", "RACE", "HISTORICAL"],
            default="OFFICIAL")
        self.src_pills.changed.connect(self._on_src_pill_change)
        src_row.addWidget(self.src_pills)

        src_row.addWidget(QLabel("Date:"))
        self.date_combo = QComboBox()
        self.date_combo.setMinimumWidth(120)
        self.date_combo.currentIndexChanged.connect(
            lambda _: self._refresh_rankings())
        self.date_combo.setEnabled(False)
        src_row.addWidget(self.date_combo)

        src_row.addWidget(QLabel("Filter:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Player name...")
        self.filter_edit.setMaximumWidth(200)
        self.filter_edit.textChanged.connect(self._on_filter)
        src_row.addWidget(self.filter_edit)

        src_row.addStretch()
        layout.addLayout(src_row)

        # --- Rankings table ---
        self.table = DataTable([
            ("Rank", 55), ("Player", 200), ("Country", 65), ("Points", 80),
            ("Age", 45), ("+/- Rank", 70), ("+/- Pts", 70),
            ("Next Tournament", 180),
        ])
        layout.addWidget(self.table, 1)

        self._populate_date_selector()

    # --- State helpers ---

    def _selected_tour(self):
        return "wta" if self._selected_category.startswith("wta") else "atp"

    def _on_cat_pill_change(self, text: str):
        cat_map = {
            "ATP Singles": "atp_singles",
            "ATP Doubles": "atp_doubles",
            "WTA Singles": "wta_singles",
            "WTA Doubles": "wta_doubles",
        }
        self._selected_category = cat_map.get(text, "atp_singles")
        self._populate_date_selector()
        self._refresh_rankings()

    def _on_src_pill_change(self, text: str):
        self._selected_source = text
        self.date_combo.setEnabled(text == "HISTORICAL")
        self._refresh_rankings()

    def _populate_date_selector(self):
        if self._selected_category.endswith("doubles"):
            self._date_map = {}
            self.date_combo.clear()
            return

        tour = self._selected_tour()
        dates = self.db.conn.execute("""
            SELECT DISTINCT ranking_date FROM rankings
            WHERE tour = ?
              AND LENGTH(ranking_date) = 8
              AND ranking_date GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
            ORDER BY ranking_date DESC
        """, (tour,)).fetchall()

        self.date_combo.blockSignals(True)
        self.date_combo.clear()
        self._date_map = {}
        for (d,) in dates:
            label = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
            self.date_combo.addItem(label)
            self._date_map[label] = d
        self.date_combo.blockSignals(False)

    def _refresh_rankings(self):
        top_n = 1000
        tour = self._selected_tour()
        discipline = "doubles" if self._selected_category.endswith("doubles") else "singles"
        category_label = self._selected_category.replace("_", " ").upper()

        if self._selected_source in ("LIVE", "RACE", "OFFICIAL"):
            # Show "Loading..." and scrape in background thread
            self.date_info_label.setText(f"{category_label} — Loading...")
            self._scrape_worker = _RankingScrapeWorker(
                self.db, tour, discipline, self._selected_source, top_n, self)
            self._scrape_worker.finished.connect(
                lambda r, d: self._on_scrape_done(r, d, category_label))
            self._scrape_worker.start()
            return
        elif self._selected_source == "HISTORICAL":
            if discipline == "doubles":
                rankings, date = [], None
            else:
                sel = self.date_combo.currentText()
                sel_date = self._date_map.get(sel)
                if sel_date:
                    rankings, date = self.db.get_rankings(
                        tour=tour, top_n=top_n, date=sel_date)
                else:
                    rankings, date = [], None
        else:
            rankings, date = [], None

        self._all_rankings = rankings

        # Date info label
        if date:
            d = str(date)
            if d.startswith("SCRAPED_"):
                self.date_info_label.setText(
                    f"{category_label} — {self._selected_source}")
            elif len(d) == 8:
                self.date_info_label.setText(
                    f"{category_label} — {d[:4]}-{d[4:6]}-{d[6:8]}")
            else:
                self.date_info_label.setText(f"{category_label} — {d}")
        else:
            self.date_info_label.setText(f"{category_label} — No ranking data")

        self._populate_table(rankings)

    def _populate_table(self, rankings):
        def _fmt_signed(value):
            if value is None or str(value).strip() == "":
                return ""
            try:
                iv = int(value)
            except (TypeError, ValueError):
                return str(value)
            return f"+{iv}" if iv > 0 else str(iv)

        rows = []
        row_colors = []  # list of (row_idx, col_idx, QColor)
        for i, r in enumerate(rankings):
            name = f"{r.get('name_first', '')} {r.get('name_last', '')}".strip()
            g = lambda k: r.get(k) if r.get(k) is not None else ""
            rank_diff = _fmt_signed(r.get("rank_diff"))
            pts_diff = _fmt_signed(r.get("pts_diff"))

            rows.append([
                str(g("rank")),
                name,
                str(g("ioc")),
                str(g("points")),
                str(g("age")),
                rank_diff,
                pts_diff,
                str(g("next_tournament")),
            ])

            # Color rank_diff column (5) and pts_diff column (6)
            for col_idx, val in [(5, r.get("rank_diff")), (6, r.get("pts_diff"))]:
                try:
                    iv = int(val)
                    if iv > 0:
                        row_colors.append((i, col_idx, QColor(COLORS["green"])))
                    elif iv < 0:
                        row_colors.append((i, col_idx, QColor(COLORS["red"])))
                except (TypeError, ValueError):
                    pass

        self.table.populate(rows)

        # Apply colors
        for r_idx, c_idx, color in row_colors:
            item = self.table.item(r_idx, c_idx)
            if item:
                item.setForeground(color)

    def _on_scrape_done(self, rankings, date, category_label):
        if rankings is None:
            self.date_info_label.setText(f"{category_label} — No ranking data")
            self._all_rankings = []
            self._populate_table([])
            return
        self._all_rankings = rankings
        if date:
            d = str(date)
            if d.startswith("SCRAPED_"):
                self.date_info_label.setText(
                    f"{category_label} — {self._selected_source}")
            elif len(d) == 8:
                self.date_info_label.setText(
                    f"{category_label} — {d[:4]}-{d[4:6]}-{d[6:8]}")
            else:
                self.date_info_label.setText(f"{category_label} — {d}")
        else:
            self.date_info_label.setText(f"{category_label} — No ranking data")
        self._populate_table(rankings)

    def stop_workers(self):
        """Stop any running background worker before page destruction."""
        if self._scrape_worker is not None and self._scrape_worker.isRunning():
            self._scrape_worker.quit()
            self._scrape_worker.wait(3000)
            self._scrape_worker = None

    def _on_filter(self, text):
        query = text.strip().lower()
        if not query:
            self._populate_table(self._all_rankings)
            return
        filtered = [
            r for r in self._all_rankings
            if query in f"{r.get('name_first', '')} {r.get('name_last', '')}".lower()
            or query in (r.get("ioc", "") or "").lower()
        ]
        self._populate_table(filtered)
