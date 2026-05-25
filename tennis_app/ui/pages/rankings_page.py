"""
Rankings browser page.
"""

from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QColor, QIcon, QPixmap

from ...core.data_manager import get_data_dir
from ..widgets import DataTable, Separator, PillButtonGroup, SectionHeader
from ..theme import COLORS


_IOC_TO_ISO2 = {
    "ALG": "DZ", "AND": "AD", "ARG": "AR", "ARM": "AM",
    "AUS": "AU", "AUT": "AT", "AZE": "AZ", "BAH": "BS",
    "BAR": "BB", "BEL": "BE", "BER": "BM", "BIH": "BA",
    "BLR": "BY", "BOL": "BO", "BRA": "BR", "BUL": "BG",
    "CAN": "CA", "CHI": "CL", "CHN": "CN", "CIV": "CI",
    "CMR": "CM", "COL": "CO", "CRC": "CR", "CRO": "HR",
    "CUB": "CU", "CYP": "CY", "CZE": "CZ", "DEN": "DK",
    "DOM": "DO", "ECU": "EC", "EGY": "EG", "ESA": "SV",
    "ESP": "ES", "EST": "EE", "FIN": "FI", "FRA": "FR",
    "GBR": "GB", "GEO": "GE", "GER": "DE", "GHA": "GH",
    "GRE": "GR", "GUA": "GT", "HKG": "HK", "HON": "HN",
    "HUN": "HU", "IND": "IN", "INA": "ID", "IRI": "IR",
    "IRL": "IE", "ISL": "IS", "ISR": "IL", "ISV": "VI",
    "ITA": "IT", "JAM": "JM", "JPN": "JP", "JOR": "JO",
    "KAZ": "KZ", "KEN": "KE", "KGZ": "KG", "KOR": "KR",
    "KOS": "XK", "KSA": "SA", "KUW": "KW", "LAT": "LV",
    "LBN": "LB", "LIE": "LI", "LTU": "LT", "LUX": "LU",
    "MAD": "MG", "MAR": "MA", "MAS": "MY", "MDA": "MD",
    "MEX": "MX", "MKD": "MK", "MGL": "MN", "MON": "MC",
    "MNE": "ME", "NAM": "NA", "NCA": "NI", "NED": "NL",
    "NGR": "NG", "NOR": "NO", "NZL": "NZ", "OMA": "OM",
    "PAK": "PK", "PAN": "PA", "PAR": "PY", "PER": "PE",
    "PHI": "PH", "POL": "PL", "POR": "PT", "PUR": "PR",
    "QAT": "QA", "ROU": "RO", "RSA": "ZA", "RUS": "RU",
    "SEN": "SN", "SGP": "SG", "SLO": "SI", "SMR": "SM",
    "SRB": "RS", "SUI": "CH", "SVK": "SK", "SWE": "SE",
    "SYR": "SY", "TAN": "TZ", "THA": "TH", "TPE": "TW",
    "TRI": "TT", "TUN": "TN", "TUR": "TR", "UAE": "AE",
    "UGA": "UG", "UKR": "UA", "URU": "UY", "USA": "US",
    "UZB": "UZ", "VEN": "VE", "VIE": "VN", "ZIM": "ZW",
}

_FLAG_ICON_CACHE: dict[str, QIcon] = {}
_FLAG_ICON_MISSING: set[str] = set()


def _country_label(ioc: str | None) -> str:
    code = str(ioc or "").strip().upper()
    return code


def _flag_cache_dir() -> Path:
    path = Path(get_data_dir()) / "flag_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _flag_icon(ioc: str | None) -> QIcon | None:
    code = str(ioc or "").strip().upper()
    if not code:
        return None
    if code in _FLAG_ICON_CACHE:
        return _FLAG_ICON_CACHE[code]
    if code in _FLAG_ICON_MISSING:
        return None

    iso2 = _IOC_TO_ISO2.get(code, code if len(code) == 2 else "")
    iso2 = iso2.strip().lower()
    if len(iso2) != 2 or not iso2.isalpha():
        _FLAG_ICON_MISSING.add(code)
        return None

    cache_path = _flag_cache_dir() / f"{iso2}.png"
    pixmap = QPixmap()
    if cache_path.exists() and pixmap.load(str(cache_path)):
        icon = QIcon(pixmap)
        _FLAG_ICON_CACHE[code] = icon
        return icon

    url = f"https://flagcdn.com/w20/{iso2}.png"
    try:
        with urlopen(url, timeout=4) as response:
            data = response.read()
        cache_path.write_bytes(data)
        if pixmap.loadFromData(data):
            icon = QIcon(pixmap)
            _FLAG_ICON_CACHE[code] = icon
            return icon
    except (OSError, URLError, TimeoutError, ValueError):
        pass

    _FLAG_ICON_MISSING.add(code)
    return None


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
            ("Rank", 55), ("Player", 200), ("Country", 88), ("Points", 80),
            ("Age", 45), ("+/- Rank", 70), ("+/- Pts", 70),
            ("Next Tournament", 180),
        ])
        self.table.setIconSize(QSize(20, 14))
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
                _country_label(r.get("ioc")),
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

        for row_idx, ranking in enumerate(rankings):
            item = self.table.item(row_idx, 2)
            if item:
                icon = _flag_icon(ranking.get("ioc"))
                if icon is not None:
                    item.setIcon(icon)

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
