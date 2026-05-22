"""Tournament-level leaderboard dashboard page."""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..theme import COLORS
from ..widgets import DataTable, PillButtonGroup, ScrollablePage, Separator
from ...core.global_stats_engine import GlobalStatsEngine


TABLE_SPECS = [
    {
        "stat_id": "most_matches_won_by_level",
        "title": "Maggior numero di vittorie totali",
        "headers": ["Rank", "Player", "Wins", ""],
        "columns": [("Rank", 46), ("Player", 180), ("Wins", 64), ("", 110)],
        "keep": [0, 1, 2, 3],
    },
    {
        "stat_id": "most_wins_at_single_tournament",
        "title": "Maggior numero di vittorie per torneo",
        "headers": ["Rank", "Player", "Wins", "Tournament"],
        "columns": [("Rank", 46), ("Player", 160), ("Wins", 64), ("Tournament", 170)],
        "keep": [0, 1, 2, 3],
    },
    {
        "stat_id": "most_finals_overall",
        "title": "Maggior numero di finali",
        "headers": ["Rank", "Player", "Finals", "Detail"],
        "columns": [("Rank", 46), ("Player", 170), ("Finals", 64), ("Detail", 120)],
        "keep": [0, 1, 2, 3],
    },
    {
        "stat_id": "most_semifinals_overall",
        "title": "Maggior numero di semifinali",
        "headers": ["Rank", "Player", "SF", "Detail"],
        "columns": [("Rank", 46), ("Player", 170), ("SF", 64), ("Detail", 120)],
        "keep": [0, 1, 2, 3],
    },
    {
        "stat_id": "title_streak_overall",
        "title": "Numero tornei vinti consecutivamente",
        "headers": ["Rank", "Player", "Titles", "Period"],
        "columns": [("Rank", 46), ("Player", 170), ("Titles", 64), ("Period", 140)],
        "keep": [0, 1, 2, 3],
    },
    {
        "stat_id": "win_streak_overall",
        "title": "Striscia vittorie consecutive",
        "headers": ["Rank", "Player", "Wins", "Period"],
        "columns": [("Rank", 46), ("Player", 170), ("Wins", 64), ("Period", 140)],
        "keep": [0, 1, 2, 3],
    },
]

LEVEL_OPTIONS = [
    "All",
    "Grand Slam",
    "Masters 1000",
    "ATP/WTA Finals",
    "Olympics",
    "ATP/WTA 500",
    "WTA 250",
    "DC/BJKC",
    "Challenger",
]


class _TournamentStatsWorker(QThread):
    data_ready = Signal(int, dict)
    error = Signal(int, str)

    def __init__(self, db, filters, request_id, parent=None):
        super().__init__(parent)
        self._db = db
        self._filters = dict(filters)
        self._request_id = request_id

    def run(self):
        try:
            engine = GlobalStatsEngine(self._db)
            payload = {}
            for spec in TABLE_SPECS:
                payload[spec["stat_id"]] = engine.compute(
                    spec["stat_id"], self._filters, limit=10)
            self.data_ready.emit(self._request_id, payload)
        except Exception as exc:
            self.error.emit(self._request_id, str(exc))


class _LeaderboardCard(QFrame):
    def __init__(self, title, columns, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame {{ background-color: {COLORS['bg_secondary']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 12px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("subHeaderLabel")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.note_label = QLabel("")
        self.note_label.setObjectName("dimLabel")
        self.note_label.setWordWrap(True)
        self.note_label.hide()
        layout.addWidget(self.note_label)

        self.table = DataTable(columns)
        self.table.setMinimumHeight(300)
        self.table.setMaximumHeight(300)
        layout.addWidget(self.table)

    def populate(self, headers, rows, note=""):
        self.table.set_column_headers(headers)
        self.table.populate(rows)
        if note:
            self.note_label.setText(note)
            self.note_label.show()
        else:
            self.note_label.hide()


class TournamentStatsPage(QWidget):
    """Desktop dashboard with tournament-level leaderboard mini tables."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._first_show = True
        self._request_id = 0
        self._workers = {}
        self._cards = {}
        self._build_ui()

    def showEvent(self, event):
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        header = QLabel("Torunaments Stats")
        header.setObjectName("headerLabel")
        layout.addWidget(header)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)

        self.tour_pills = PillButtonGroup(["ATP", "WTA"])
        self.tour_pills.changed.connect(lambda _: self._refresh())
        filter_row.addWidget(self.tour_pills)

        filter_row.addWidget(QLabel("Level:"))
        self.level_combo = QComboBox()
        self.level_combo.addItems(LEVEL_OPTIONS)
        self.level_combo.currentIndexChanged.connect(lambda _: self._refresh())
        filter_row.addWidget(self.level_combo)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("dimLabel")
        layout.addWidget(self.status_label)
        layout.addWidget(Separator())

        self.scroll = ScrollablePage()
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)

        for index, spec in enumerate(TABLE_SPECS):
            card = _LeaderboardCard(spec["title"], spec["columns"], parent=self)
            self._cards[spec["stat_id"]] = card
            grid.addWidget(card, index // 2, index % 2)

        self.scroll.content_layout.addWidget(grid_host)
        layout.addWidget(self.scroll, 1)

    def _current_filters(self):
        filters = {"tour": self.tour_pills.value().lower()}
        level = self.level_combo.currentText()
        if level and level != "All":
            filters["level"] = level
        return filters

    def _refresh(self):
        self._request_id += 1
        request_id = self._request_id
        filters = self._current_filters()
        self.status_label.setText("Loading tournament stats...")

        worker = _TournamentStatsWorker(self.db, filters, request_id, parent=self)
        worker.data_ready.connect(self._on_data_ready)
        worker.error.connect(self._on_error)
        worker.finished.connect(lambda rid=request_id: self._workers.pop(rid, None))
        self._workers[request_id] = worker
        worker.start()

    def _normalize_result(self, spec, result):
        rows = result.get("rows") or []
        keep = spec["keep"]
        normalized = []
        for row in rows[:10]:
            normalized.append([
                row[index] if index < len(row) else ""
                for index in keep
            ])
        if not normalized:
            normalized = [["", "No data", "", ""]]
        return spec["headers"], normalized, result.get("note") or ""

    def _on_data_ready(self, request_id, payload):
        if request_id != self._request_id:
            return
        for spec in TABLE_SPECS:
            result = payload.get(spec["stat_id"], {})
            headers, rows, note = self._normalize_result(spec, result)
            self._cards[spec["stat_id"]].populate(headers, rows, note)
        self.status_label.setText("")

    def _on_error(self, request_id, message):
        if request_id != self._request_id:
            return
        self.status_label.setText(f"Error loading tournament stats: {message}")