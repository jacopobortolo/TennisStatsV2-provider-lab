"""
Match Detail Dialog — shows full statistics for a single match.

Opened on double-click from MatchesPage, H2HPage and TournamentsPage.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QWidget, QSizePolicy, QFrame,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWebEngineWidgets import QWebEngineView

from .widgets import (
    ScrollablePage, StatCard, StatGrid, SectionHeader, Separator, DataTable,
    ComparisonBar,
)
from .charts import (
    spider_chart_dual, pressure_chart, get_chart_base_url,
)
from .theme import COLORS, FONTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_date(d):
    d = str(d or "")
    if len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def _pct(v):
    if v is None:
        return "–"
    try:
        return f"{float(v):.1f}%"
    except (ValueError, TypeError):
        return "–"


def _n(v):
    if v is None:
        return "–"
    try:
        return str(int(float(v)))
    except (ValueError, TypeError):
        return "–"


def _ratio(v):
    if v is None:
        return "–"
    try:
        return f"{float(v):.2f}"
    except (ValueError, TypeError):
        return "–"


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------

class MatchDetailDialog(QDialog):
    """Modale con statistiche complete di un singolo match."""

    def __init__(self, db, match: dict, parent=None):
        super().__init__(parent)
        self.db = db
        self.match = match
        self.setWindowTitle("Match Detail")
        self.resize(920, 780)
        self.setMinimumSize(700, 500)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Title bar ────────────────────────────────────────────────────
        title_bar = QWidget()
        title_bar.setStyleSheet(
            f"background-color: {COLORS['bg_secondary']};"
            f"border-bottom: 1px solid {COLORS['border']};"
        )
        tb_layout = QHBoxLayout(title_bar)
        tb_layout.setContentsMargins(24, 14, 24, 14)
        tb_layout.setSpacing(12)

        self._build_header(tb_layout)
        root.addWidget(title_bar)

        # ── Scrollable body ───────────────────────────────────────────────
        self._body = ScrollablePage()
        root.addWidget(self._body, 1)

        # ── Close button ─────────────────────────────────────────────────
        btn_bar = QWidget()
        btn_bar.setStyleSheet(
            f"background-color: {COLORS['bg_secondary']};"
            f"border-top: 1px solid {COLORS['border']};"
        )
        btn_layout = QHBoxLayout(btn_bar)
        btn_layout.setContentsMargins(16, 10, 16, 10)
        btn_layout.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setObjectName("accentBtn")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        root.addWidget(btn_bar)

        # ── Populate content ──────────────────────────────────────────────
        self._populate_body()

    # ------------------------------------------------------------------
    # Title bar
    # ------------------------------------------------------------------

    def _build_header(self, layout):
        m = self.match
        winner = m.get("winner_name", "?")
        loser = m.get("loser_name", "?")
        score = m.get("score") or "–"
        rnd = m.get("round") or ""
        tourney = m.get("tourney_name") or ""
        date = _fmt_date(m.get("tourney_date"))
        surface = m.get("surface") or ""
        minutes = m.get("minutes")
        level_map = {
            "G": "Grand Slam", "M": "Masters", "A": "ATP",
            "D": "Davis Cup", "F": "Finals", "C": "Challenger",
        }
        level = level_map.get(m.get("tourney_level", ""), m.get("tourney_level", ""))

        # Left: title block
        left = QVBoxLayout()
        left.setSpacing(4)

        match_title = QLabel(f"{winner}  def.  {loser}  —  {score}")
        match_title.setStyleSheet(
            f"font-size: {FONTS['size_xl']}pt; font-weight: 700;"
            f" color: {COLORS['text']}; background: transparent;"
        )
        left.addWidget(match_title)

        sub_parts = [p for p in [tourney, rnd, date, surface, level] if p]
        if minutes:
            try:
                sub_parts.append(f"{int(float(minutes))} min")
            except (ValueError, TypeError):
                pass
        subtitle = QLabel("  ·  ".join(sub_parts))
        subtitle.setStyleSheet(
            f"color: {COLORS['text_dim']}; background: transparent;"
        )
        left.addWidget(subtitle)

        left_w = QWidget()
        left_w.setStyleSheet("background: transparent;")
        left_w.setLayout(left)
        layout.addWidget(left_w)
        layout.addStretch()

    # ------------------------------------------------------------------
    # Body builder
    # ------------------------------------------------------------------

    def _populate_body(self):
        m = self.match
        is_upcoming = bool(m.get("is_upcoming"))

        layout = self._body.content_layout
        layout.setSpacing(20)

        # ── Scheduled placeholder ─────────────────────────────────────
        if is_upcoming:
            lbl = QLabel("⏳  Scheduled match — no statistics yet.")
            lbl.setObjectName("subHeaderLabel")
            lbl.setAlignment(Qt.AlignCenter)
            layout.addWidget(lbl)
            layout.addStretch()
            return

        # ── Resolve player IDs ────────────────────────────────────────
        winner_name = m.get("winner_name", "")
        loser_name = m.get("loser_name", "")
        winner_id = m.get("winner_id", "")
        loser_id = m.get("loser_id", "")

        # If IDs are not stored on the match dict, look them up
        if not winner_id:
            res = self.db.search_players(winner_name, limit=1)
            winner_id = res[0]["player_id"] if res else ""
        if not loser_id:
            res = self.db.search_players(loser_name, limit=1)
            loser_id = res[0]["player_id"] if res else ""

        # Patch a copy of m so compute_match_stats can resolve perspective
        # correctly even when the DB row has empty winner_id/loser_id.
        m_ids = dict(m)
        if winner_id:
            m_ids["winner_id"] = winner_id
        if loser_id:
            m_ids["loser_id"] = loser_id

        # ── Compute box-score derived stats ───────────────────────────
        from ..core.stats_engine import compute_match_stats

        w_stats = compute_match_stats(m_ids, winner_id) if winner_id else {}
        l_stats = compute_match_stats(m_ids, loser_id) if loser_id else {}

        has_boxscore = bool(
            m.get("w_svpt") or m.get("l_svpt") or
            m.get("w_ace") or m.get("l_ace")
        )

        # ── Box Score Section ─────────────────────────────────────────
        if has_boxscore:
            layout.addWidget(SectionHeader("Box Score"))
            layout.addWidget(self._build_boxscore(winner_name, w_stats,
                                                   loser_name, l_stats))
            layout.addWidget(Separator())

        # ── Spider Chart ──────────────────────────────────────────────
        spider_html = self._build_spider(winner_name, w_stats,
                                         loser_name, l_stats)
        if spider_html:
            layout.addWidget(SectionHeader("Performance Comparison"))
            chart = _make_webview(spider_html, height=380)
            layout.addWidget(chart)
            layout.addWidget(Separator())

        # ── Pressure Chart ────────────────────────────────────────────
        pressure_html = self._build_pressure(w_stats, l_stats,
                                              winner_name, loser_name)
        if pressure_html:
            layout.addWidget(SectionHeader("Pressure Metrics"))
            chart = _make_webview(pressure_html, height=260)
            layout.addWidget(chart)
            layout.addWidget(Separator())

        # ── Extended Stats ────────────────────────────────────────────
        tourney_name = m.get("tourney_name", "")
        tourney_date = str(m.get("tourney_date", ""))
        ext = self.db.get_match_extended_stats(
            winner_name, loser_name, tourney_name, tourney_date)

        has_extended = any(v is not None for v in ext.values())
        if has_extended:
            layout.addWidget(SectionHeader("Extended Statistics"))
            self._build_extended(layout, ext, winner_name, loser_name)

        layout.addStretch()

    # ------------------------------------------------------------------
    # Box-score widget (two-column comparison bars)
    # ------------------------------------------------------------------

    def _build_boxscore(self, w_name, w_stats, l_name, l_stats):
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        col_layout = QVBoxLayout(container)
        col_layout.setContentsMargins(0, 0, 0, 0)
        col_layout.setSpacing(6)

        # Header row with player names
        header = QHBoxLayout()
        lbl_w = QLabel(w_name.split()[-1] if w_name else "Winner")
        lbl_w.setStyleSheet(
            f"color: {COLORS['accent']}; font-weight: 700;"
            " background: transparent;")
        lbl_w.setAlignment(Qt.AlignRight)
        lbl_l = QLabel(l_name.split()[-1] if l_name else "Loser")
        lbl_l.setStyleSheet(
            f"color: {COLORS['red']}; font-weight: 700;"
            " background: transparent;")
        lbl_l.setAlignment(Qt.AlignLeft)
        header.addWidget(lbl_w)
        header.addStretch()
        header.addWidget(lbl_l)
        hdr_w = QWidget()
        hdr_w.setStyleSheet("background: transparent;")
        hdr_w.setLayout(header)
        col_layout.addWidget(hdr_w)

        # Stat rows
        METRICS = [
            ("Aces",            "aces",                 False, _n),
            ("Double Faults",   "double_faults",        True,  _n),
            ("1st Serve %",     "first_serve_pct",      False, _pct),
            ("1st Srv Won %",   "first_serve_won_pct",  False, _pct),
            ("2nd Srv Won %",   "second_serve_won_pct", False, _pct),
            ("Srv Pts Won %",   "total_serve_won_pct",  False, _pct),
            ("Ret Pts Won %",   "return_pts_won_pct",   False, _pct),
            ("BP Saved",        "bp_save_pct",          False, _pct),
            ("BP Conv",         "bp_conversion_pct",    False, _pct),
            ("Pts Won %",       "total_points_won_pct", False, _pct),
            ("Hold %",          "hold_pct",             False, _pct),
            ("Dom. Ratio",      "dominance_ratio",      False, _ratio),
        ]

        for label, key, invert, fmt_fn in METRICS:
            v1 = w_stats.get(key)
            v2 = l_stats.get(key)
            if v1 is None and v2 is None:
                continue
            try:
                f1 = float(v1) if v1 is not None else 0.0
                f2 = float(v2) if v2 is not None else 0.0
            except (ValueError, TypeError):
                continue
            bar = ComparisonBar(
                label, f1, f2,
                p1_color=COLORS["red"] if invert else COLORS["accent"],
                p2_color=COLORS["accent"] if invert else COLORS["red"],
            )
            col_layout.addWidget(bar)

        return container

    # ------------------------------------------------------------------
    # Spider chart
    # ------------------------------------------------------------------

    def _build_spider(self, w_name, w_stats, l_name, l_stats):
        AXES = [
            ("1st Srv %",   "first_serve_pct"),
            ("1st Srv Won", "first_serve_won_pct"),
            ("2nd Srv Won", "second_serve_won_pct"),
            ("Ret Pts Won", "return_pts_won_pct"),
            ("BP Saved %",  "bp_save_pct"),
            ("Hold %",      "hold_pct"),
        ]
        cats, vals_w, vals_l = [], [], []
        for label, key in AXES:
            v1 = _safe_float(w_stats.get(key))
            v2 = _safe_float(l_stats.get(key))
            if v1 == 0.0 and v2 == 0.0:
                continue
            cats.append(label)
            vals_w.append(v1)
            vals_l.append(v2)

        if len(cats) < 3:
            return ""
        return spider_chart_dual(cats, vals_w, w_name, vals_l, l_name,
                                 max_val=100, height=380)

    # ------------------------------------------------------------------
    # Pressure chart — per-match version using computed stats
    # ------------------------------------------------------------------

    def _build_pressure(self, w_stats, l_stats, w_name, l_name):
        # For a single match we can only show DR and BP conversion; skip
        # deciding-set / tiebreak (they require a career dataset).
        pressure = {}
        dr = w_stats.get("dominance_ratio")
        if dr is not None:
            pressure["dominance_ratio"] = float(dr)
        bpc = w_stats.get("bp_conversion_pct")
        if bpc is not None:
            pressure["breakpoints_prevail"] = float(bpc)
        bps = w_stats.get("bp_save_pct")
        if bps is not None:
            # Re-use the deciding_set_win_pct slot for "BP saved %" label
            pass  # pressure_chart only plots the 4 fixed keys

        if not pressure:
            return ""
        return pressure_chart(pressure, height=240)

    # ------------------------------------------------------------------
    # Extended stats tables
    # ------------------------------------------------------------------

    def _build_extended(self, layout, ext, w_name, l_name):
        SECTION_DEFS = [
            ("Winners & Errors",  "winners_errors",  _we_columns),
            ("Serve Speed",       "serve_speed",      _ss_columns),
            ("Point-by-Point",    "pbp",              _pbp_columns),
            ("MCP Serve",         "mcp_serve",        _mcp_serve_columns),
            ("MCP Return",        "mcp_return",       _mcp_return_columns),
            ("MCP Rally",         "mcp_rally",        _mcp_rally_columns),
            ("MCP Tactics",       "mcp_tactics",      _mcp_tactics_columns),
        ]

        for section_label, key, col_fn in SECTION_DEFS:
            row_data = ext.get(key)
            if row_data is None:
                continue

            perspective = row_data.get("_perspective", "player")
            player_label = w_name if perspective == "player" else l_name

            layout.addWidget(SectionHeader(section_label))
            cols, rows = col_fn(row_data, player_label)
            if not rows:
                continue
            tbl = DataTable(cols)
            tbl.populate(rows)
            tbl.setFixedHeight(max(90, 35 + 30 * len(rows)))
            layout.addWidget(tbl)


# ---------------------------------------------------------------------------
# Column extractors for each extended table
# ---------------------------------------------------------------------------

def _we_columns(row, player_name):
    cols = [
        ("Metric", 200), ("Value", 120),
    ]
    FIELDS = [
        ("Winners",         "winners"),
        ("Unforced Errors", "unforced_errors"),
        ("W/UE Ratio",      "w_ue_ratio"),
        ("Winner %",        "winner_pct"),
        ("UE %",            "ue_pct"),
    ]
    rows = []
    for label, key in FIELDS:
        v = row.get(key)
        if v is not None:
            try:
                rows.append([label, f"{float(v):.2f}"])
            except (ValueError, TypeError):
                rows.append([label, str(v)])
    return cols, rows


def _ss_columns(row, player_name):
    cols = [
        ("Metric", 220), ("Avg", 90), ("Max", 90), ("Min", 90),
    ]
    FIELDS = [
        ("1st Serve Speed",    "first_serve_avg",    "first_serve_max",    "first_serve_min"),
        ("2nd Serve Speed",    "second_serve_avg",   "second_serve_max",   "second_serve_min"),
    ]
    rows = []
    for label, avg_k, max_k, min_k in FIELDS:
        avg = row.get(avg_k)
        if avg is not None:
            try:
                rows.append([
                    label,
                    f"{float(avg):.1f}",
                    f"{float(row[max_k]):.1f}" if row.get(max_k) is not None else "–",
                    f"{float(row[min_k]):.1f}" if row.get(min_k) is not None else "–",
                ])
            except (ValueError, TypeError):
                pass
    return cols, rows


def _pbp_columns(row, player_name):
    cols = [
        ("Metric", 240), ("Value", 120),
    ]
    FIELDS = [
        ("Aggressive Margin", "aggressive_margin"),
        ("Serve +1 Ratio",    "serve_plus1_ratio"),
        ("Baseline %",        "baseline_pct"),
        ("Rally Length Avg",  "rally_length_avg"),
    ]
    rows = []
    for label, key in FIELDS:
        v = row.get(key)
        if v is not None:
            try:
                rows.append([label, f"{float(v):.2f}"])
            except (ValueError, TypeError):
                rows.append([label, str(v)])
    return cols, rows


def _mcp_serve_columns(row, player_name):
    cols = [("Metric", 240), ("Value", 120)]
    FIELDS = [
        ("Deuce Wide %",   "deuce_wide_pct"),
        ("Deuce Body %",   "deuce_body_pct"),
        ("Deuce T %",      "deuce_t_pct"),
        ("Ad Wide %",      "ad_wide_pct"),
        ("Ad Body %",      "ad_body_pct"),
        ("Ad T %",         "ad_t_pct"),
        ("Ace %",          "ace_pct"),
        ("Unreturned %",   "unreturned_pct"),
    ]
    rows = []
    for label, key in FIELDS:
        v = row.get(key)
        if v is not None:
            try:
                rows.append([label, f"{float(v):.1f}%"])
            except (ValueError, TypeError):
                rows.append([label, str(v)])
    return cols, rows


def _mcp_return_columns(row, player_name):
    cols = [("Metric", 240), ("Value", 120)]
    FIELDS = [
        ("Return In Play %",     "return_in_play_pct"),
        ("Return Depth Deep %",  "return_depth_deep_pct"),
        ("Return FH %",          "return_fh_pct"),
        ("Return BH %",          "return_bh_pct"),
    ]
    rows = []
    for label, key in FIELDS:
        v = row.get(key)
        if v is not None:
            try:
                rows.append([label, f"{float(v):.1f}%"])
            except (ValueError, TypeError):
                rows.append([label, str(v)])
    return cols, rows


def _mcp_rally_columns(row, player_name):
    cols = [("Metric", 240), ("Value", 120)]
    FIELDS = [
        ("Rally Length Avg",   "rally_length_avg"),
        ("FH %",               "fh_pct"),
        ("BH %",               "bh_pct"),
        ("Net Approach %",     "net_approach_pct"),
        ("Rally Won 0-4 %",    "rally_won_0_4"),
        ("Rally Won 5-8 %",    "rally_won_5_8"),
        ("Rally Won 9+ %",     "rally_won_9_plus"),
    ]
    rows = []
    for label, key in FIELDS:
        v = row.get(key)
        if v is not None:
            try:
                rows.append([label, f"{float(v):.2f}"])
            except (ValueError, TypeError):
                rows.append([label, str(v)])
    return cols, rows


def _mcp_tactics_columns(row, player_name):
    cols = [("Metric", 240), ("Value", 120)]
    FIELDS = [
        ("Net Approach %",       "net_approach_pct"),
        ("Dropshot %",           "dropshot_pct"),
        ("Serve & Volley %",     "serve_and_volley_pct"),
        ("Inside-In FH %",       "inside_in_fh_pct"),
        ("Inside-Out FH %",      "inside_out_fh_pct"),
    ]
    rows = []
    for label, key in FIELDS:
        v = row.get(key)
        if v is not None:
            try:
                rows.append([label, f"{float(v):.1f}%"])
            except (ValueError, TypeError):
                rows.append([label, str(v)])
    return cols, rows


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _make_webview(html: str, height: int = 360) -> QWebEngineView:
    chart = QWebEngineView()
    chart.page().setBackgroundColor(QColor(COLORS["bg_primary"]))
    chart.setFixedHeight(height)
    chart.setSizePolicy(
        QWebEngineView.sizePolicy(chart).horizontalPolicy(),
        chart.sizePolicy().verticalPolicy(),
    )
    chart.setHtml(html, get_chart_base_url())
    return chart
