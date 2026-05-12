"""
Main window — sidebar navigation + stacked pages + background workers.
"""

import logging
import threading
from datetime import datetime

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QStackedWidget, QLabel, QProgressBar,
    QMessageBox, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal, QSize

from .theme import COLORS, FONTS, load_fonts
from ..core.data_manager import (
    download_atp_data, download_wta_data,
    scrape_top_players_matches, scrape_current_rankings,
    scrape_top_players_extended_stats,
)
from ..core.database import TennisDatabase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class DataWorker(QThread):
    """Generic background worker that runs a callable and emits signals."""
    progress = Signal(int, str)   # (percentage, message)
    finished = Signal(bool, str)  # (success, message)

    def __init__(self, func, parent=None):
        super().__init__(parent)
        self._func = func

    def run(self):
        try:
            self._func(self._emit_progress)
            self.finished.emit(True, "")
        except Exception as exc:
            logger.exception("Worker error")
            self.finished.emit(False, str(exc))

    def _emit_progress(self, current, total, msg):
        pct = int(current / total * 100) if total else 0
        self.progress.emit(pct, msg)


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

NAV_ITEMS = [
    ("🔍  Player",       "player"),
    ("📊  Rankings",     "rankings"),
    ("🎾  Matches",      "matches"),
    ("⚔️  H2H",          "h2h"),
    ("🏆  Tournaments",  "tournaments"),
    ("📈  Stats",        "stats"),
    ("🌐  Global",       "global_stats"),
    ("🌍  Nations",      "nations"),
]


class MainWindow(QMainWindow):
    """Application main window with sidebar and stacked pages."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tennis Analytics — Powered by Jeff Sackmann's Data")
        self.resize(1280, 820)
        self.setMinimumSize(960, 600)

        # Load bundled fonts
        load_fonts()

        self.db = None
        self._pages = {}
        self._page_cls = {}
        self._nav_buttons = {}
        self._current_page = None
        self._ext_stop_event = threading.Event()
        self._ext_worker = None

        self._build_ui()

        # Start data loading
        self._init_data()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Top bar (progress + status)
        self._build_top_bar(root_layout)

        # Body: sidebar + pages
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._build_sidebar(body)
        self._build_content(body)

        root_layout.addLayout(body, 1)

    def _build_top_bar(self, parent_layout):
        bar = QWidget()
        bar.setObjectName("topBar")
        bar.setFixedHeight(48)
        bar.setStyleSheet(
            f"QWidget#topBar {{ background-color: {COLORS['bg_secondary']};"
            f" border-bottom: 1px solid {COLORS['border']}; }}")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(20, 0, 20, 0)

        # Title
        title = QLabel("🎾 Tennis Analytics")
        title.setStyleSheet(
            f"font-size: {FONTS['size_xl']}pt; font-weight: 800; "
            f"color: {COLORS['accent']}; background: transparent;"
            f" letter-spacing: -0.5px;")
        bar_layout.addWidget(title)
        bar_layout.addStretch()

        # Status label
        self.status_label = QLabel("Initializing...")
        self.status_label.setObjectName("dimLabel")
        self.status_label.setStyleSheet("background: transparent;")
        bar_layout.addWidget(self.status_label)

        # Refresh button
        self.refresh_btn = QPushButton("⟳ Refresh Data")
        self.refresh_btn.setObjectName("accentBtn")
        self.refresh_btn.clicked.connect(self._refresh_data)
        bar_layout.addWidget(self.refresh_btn)

        # Scrape button
        self.scrape_btn = QPushButton("🌐 Scrape Live Data")
        self.scrape_btn.setObjectName("accentBtn")
        self.scrape_btn.clicked.connect(self._scrape_live_data)
        bar_layout.addWidget(self.scrape_btn)

        # Manual extended-stats refresh (cloud handles this hourly;
        # this button forces a local re-scrape on demand).
        self.ext_btn = QPushButton("📊 Refresh Extended Stats")
        self.ext_btn.setObjectName("accentBtn")
        self.ext_btn.setToolTip(
            "Manually scrape extended stats for the top 150 players.\n"
            "Normally this data comes from the cloud sync at startup.")
        self.ext_btn.clicked.connect(self._start_background_extended_scrape)
        bar_layout.addWidget(self.ext_btn)

        parent_layout.addWidget(bar)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setValue(0)
        parent_layout.addWidget(self.progress_bar)

    def _build_sidebar(self, parent_layout):
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 20, 10, 20)
        sidebar_layout.setSpacing(4)
        self._sidebar_layout = sidebar_layout

        for label, key in NAV_ITEMS:
            btn = QPushButton(label)
            btn.setObjectName("navButton")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, k=key: self._switch_page(k))
            sidebar_layout.addWidget(btn)
            self._nav_buttons[key] = btn

        self._global_records_host = QWidget()
        self._global_records_host.setVisible(False)
        self._global_records_layout = QVBoxLayout(self._global_records_host)
        self._global_records_layout.setContentsMargins(0, 10, 0, 0)
        self._global_records_layout.setSpacing(0)
        sidebar_layout.addWidget(self._global_records_host, 1)

        sidebar_layout.addStretch()

        parent_layout.addWidget(sidebar)

    def _build_content(self, parent_layout):
        self.stack = QStackedWidget()
        self.stack.setObjectName("contentStack")
        self.stack.setStyleSheet(
            f"QStackedWidget#contentStack {{ background-color: {COLORS['bg_primary']}; }}")

        # Loading placeholder
        self._loading_widget = QLabel("Loading data, please wait...")
        self._loading_widget.setObjectName("headerLabel")
        self._loading_widget.setAlignment(Qt.AlignCenter)
        self.stack.addWidget(self._loading_widget)

        parent_layout.addWidget(self.stack, 1)

    # ------------------------------------------------------------------
    # Page management
    # ------------------------------------------------------------------

    def _build_pages(self):
        """Register page classes for lazy construction on first visit."""
        from .pages.player_page import PlayerPage
        from .pages.rankings_page import RankingsPage
        from .pages.matches_page import MatchesPage
        from .pages.h2h_page import H2HPage
        from .pages.tournaments_page import TournamentsPage
        from .pages.stats_page import StatsPage
        from .pages.insights_page import InsightsPage
        from .pages.global_stats_page import GlobalStatsPage

        self._page_cls = {
            "player": PlayerPage,
            "rankings": RankingsPage,
            "matches": MatchesPage,
            "h2h": H2HPage,
            "tournaments": TournamentsPage,
            "stats": StatsPage,
            "global_stats": GlobalStatsPage,
            "nations": InsightsPage,
        }

        # Remove loading placeholder (only on first build)
        if self._loading_widget is not None:
            try:
                self.stack.removeWidget(self._loading_widget)
                self._loading_widget.deleteLater()
            except RuntimeError:
                pass
            self._loading_widget = None

        # Build only the landing page; all others are built on first visit
        self._switch_page("player")

    def _ensure_page(self, key):
        """Build a page lazily on first access."""
        if key in self._pages:
            return
        cls = self._page_cls.get(key)
        if not cls:
            return
        page = cls(self.db)
        self._pages[key] = page
        self.stack.addWidget(page)
        if key == "matches" and hasattr(page, "navigate_to_tournament"):
            page.navigate_to_tournament.connect(self._on_navigate_to_tournament)
        if key == "nations" and hasattr(page, "navigate_to_tournament"):
            page.navigate_to_tournament.connect(self._on_navigate_to_tournament)

    def _attach_global_records_panel(self):
        page = self._pages.get("global_stats")
        if not page or not hasattr(page, "records_panel"):
            return
        panel = page.records_panel()
        if panel.parent() is not self._global_records_host:
            self._global_records_layout.addWidget(panel)
        panel.setVisible(True)

    def _on_navigate_to_tournament(self, tourney_name: str, year_str: str,
                                   tour: str):
        """Called when user double-clicks a tournament in the Matches page."""
        self._ensure_page("tournaments")
        tourney_page = self._pages.get("tournaments")
        if tourney_page and hasattr(tourney_page, "navigate_to"):
            tourney_page.navigate_to(tourney_name, year_str, tour)
        self._switch_page("tournaments")

    def _switch_page(self, key):
        self._ensure_page(key)
        if key not in self._pages:
            return
        self._current_page = key
        self.stack.setCurrentWidget(self._pages[key])

        if key == "global_stats":
            self._attach_global_records_panel()
        self._global_records_host.setVisible(key == "global_stats")

        # Update nav button states
        for k, btn in self._nav_buttons.items():
            btn.setProperty("active", k == key)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def switch_to_stats(self, player_name: str):
        """Navigate to the Stats page and load a player by name."""
        self._switch_page("stats")
        stats_page = self._pages.get("stats")
        if not stats_page:
            return
        # Resolve name → player record via DB so we can call _load_player
        # directly (the search widget API uses player_selected, not text).
        try:
            results = self.db.search_players(player_name, limit=1)
        except Exception:
            results = []
        if not results:
            return
        player = results[0]
        # Reflect the choice in the search field for visual consistency
        if hasattr(stats_page, "player_edit") and hasattr(
                stats_page.player_edit, "set_text"):
            stats_page.player_edit.set_text(player.get("name", player_name))
        stats_page._load_player(player)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _init_data(self):
        worker = DataWorker(self._load_data_task, self)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_data_loaded)
        self._worker = worker
        worker.start()

    def _load_data_task(self, progress_cb):
        self.db = TennisDatabase()

        need_atp = not self.db.has_data("atp")
        need_wta = not self.db.has_data("wta")

        if need_atp:
            progress_cb(0, 100, "Downloading ATP data...")
            download_atp_data(
                year_start=1968,
                progress_callback=lambda c, t, m: progress_cb(c, t * 2, m),
            )
            progress_cb(50, 100, "Importing ATP data...")
            self.db.import_data(
                "atp", year_start=1968,
                progress_callback=lambda c, t, m: progress_cb(50 + c, t * 2, m),
            )
        elif not self.db.has_doubles_data("atp"):
            progress_cb(0, 100, "Importing ATP doubles...")
            self.db.import_doubles_only("atp")

        if need_wta:
            progress_cb(50, 100, "Downloading WTA data...")
            download_wta_data(
                year_start=1968,
                progress_callback=lambda c, t, m: progress_cb(c, t * 2, m),
            )
            progress_cb(75, 100, "Importing WTA data...")
            self.db.import_data(
                "wta", year_start=1968,
                progress_callback=lambda c, t, m: progress_cb(75 + c, t * 4, m),
            )

        progress_cb(100, 100, "Ready")

    def _on_progress(self, pct, msg):
        self.progress_bar.setValue(pct)
        self.status_label.setText(msg)

    def _on_data_loaded(self, success, error_msg):
        self.progress_bar.setValue(0)
        if success:
            self.status_label.setText("Ready")
            self._build_pages()
        else:
            self.status_label.setText("Error loading data")
            QMessageBox.critical(self, "Error",
                                 f"Failed to load data:\n{error_msg}")

    # ------------------------------------------------------------------
    # Refresh / Scrape
    # ------------------------------------------------------------------

    def _refresh_data(self):
        self.refresh_btn.setEnabled(False)
        worker = DataWorker(self._refresh_task, self)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_refresh_done)
        self._worker = worker
        worker.start()

    def _refresh_task(self, progress_cb):
        download_atp_data(
            year_start=1968, force=True,
            progress_callback=lambda c, t, m: progress_cb(c, t * 4, m),
        )
        download_wta_data(
            year_start=1968, force=True,
            progress_callback=lambda c, t, m: progress_cb(25 + c, t * 4, m),
        )
        if not self.db:
            self.db = TennisDatabase()
        self.db.import_data(
            "atp", year_start=1968,
            progress_callback=lambda c, t, m: progress_cb(50 + c, t * 4, m),
        )
        self.db.import_data(
            "wta", year_start=1968,
            progress_callback=lambda c, t, m: progress_cb(75 + c, t * 4, m),
        )

    def _on_refresh_done(self, success, error_msg):
        self.refresh_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        if success:
            self.status_label.setText("Data refreshed!")
            if self.db:
                self.db.invalidate_player_cache()
            self._rebuild_pages()
        else:
            QMessageBox.critical(self, "Error",
                                 f"Refresh failed:\n{error_msg}")

    def _scrape_live_data(self):
        self.scrape_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        # If a manual extended-stats scrape is running, pause it so it
        # doesn't fight the live scraper for the SQLite write lock.
        self._ext_stop_event.set()
        if self._ext_worker and self._ext_worker.isRunning():
            self._ext_worker.quit()
            self._ext_worker.wait(5000)
        self._ext_worker = None
        worker = DataWorker(self._scrape_task, self)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_scrape_done)
        self._worker = worker
        worker.start()

    def _scrape_task(self, progress_cb):
        if not self.db:
            self.db = TennisDatabase()

        is_monday = datetime.today().weekday() == 0
        top_n = 1000 if is_monday else 150

        for tour in ("atp", "wta"):
            label = tour.upper()
            progress_cb(0, 100, f"Scraping {label} live match data...")

            matches_df, rankings, scraped_names = scrape_top_players_matches(
                top_n=top_n, tour=tour,
                progress_callback=lambda c, t, m: progress_cb(c, t, m),
                db=self.db,
                cache_expire_hours=6,
                min_year=2025,
            )

            if not matches_df.empty:
                progress_cb(80, 100, f"Importing {label} scraped matches...")
                self.db.import_scraped_matches(
                    matches_df, scraped_player_names=scraped_names,
                    replace_existing=False)

            if rankings:
                progress_cb(90, 100, f"Importing {label} live rankings...")
                self.db.import_scraped_rankings(rankings)

    def _on_scrape_done(self, success, error_msg):
        self.scrape_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        if success:
            count = self.db.get_scraped_match_count() if self.db else 0
            self.status_label.setText(
                f"Live data loaded! ({count} scraped matches)")
            if self.db:
                self.db.invalidate_player_cache()
            self._rebuild_pages()
        else:
            QMessageBox.critical(self, "Error",
                                 f"Scraping failed:\n{error_msg}")
        # Note: extended stats are no longer auto-scraped here.
        # They are kept in sync via the cloud workflow (hourly) and
        # merged into the local DB at app startup. Use the "Refresh
        # Extended Stats" button if you need a manual local re-scrape.

    def _rebuild_pages(self):
        """Destroy and recreate all pages to reflect new data."""
        prev_page = getattr(self, "_current_page", "player")
        for key, page in self._pages.items():
            # Stop any background threads owned by the page
            if hasattr(page, "stop_workers"):
                page.stop_workers()
            self.stack.removeWidget(page)
            page.deleteLater()
        self._pages.clear()
        self._build_pages()
        self._switch_page(prev_page)

    # ------------------------------------------------------------------
    # Manual extended stats scraping
    # ------------------------------------------------------------------

    def _start_background_extended_scrape(self):
        """Manually scrape extended stats for top 150 players (per tour).

        Triggered by the toolbar button only. The cloud workflow runs
        the same scrape hourly and the result is merged at app startup,
        so this is a fallback / on-demand refresh.
        """
        if not self.db:
            return
        if self._ext_worker and self._ext_worker.isRunning():
            self.status_label.setText("Extended stats scrape already running")
            return
        self.ext_btn.setEnabled(False)
        self._ext_stop_event.clear()
        self._ext_worker = DataWorker(self._ext_scrape_task, self)
        self._ext_worker.progress.connect(self._on_ext_progress)
        self._ext_worker.finished.connect(self._on_ext_done)
        self._ext_worker.start()

    def _ext_scrape_task(self, progress_cb):
        if not self.db:
            return
        for tour in ("atp", "wta"):
            if self._ext_stop_event.is_set():
                break
            scrape_top_players_extended_stats(
                top_n=150, tour=tour,
                progress_callback=lambda c, t, m: progress_cb(c, t, m),
                db=self.db,
                stop_event=self._ext_stop_event,
            )

    def _on_ext_progress(self, pct, msg):
        self.status_label.setText(f"🔄 {msg}")

    def _on_ext_done(self, success, error_msg):
        self.ext_btn.setEnabled(True)
        if success:
            self.status_label.setText("Ready — extended stats updated")
            self._rebuild_pages()
        else:
            logger.warning("Extended stats scrape failed: %s", error_msg)
            self.status_label.setText("Ready")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._ext_stop_event.set()
        if self._ext_worker and self._ext_worker.isRunning():
            self._ext_worker.quit()
            self._ext_worker.wait(3000)
        if self.db:
            self.db.close()
        event.accept()
