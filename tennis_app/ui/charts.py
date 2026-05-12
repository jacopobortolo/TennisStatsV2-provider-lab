"""
Plotly chart generators — return HTML strings ready for QWebEngineView.

Each function builds a Plotly figure, converts it to a self-contained HTML
string (with plotly.js served from a local file), and returns it.
"""

import json
import logging
from pathlib import Path

import plotly
import plotly.graph_objects as go
from PySide6.QtCore import QUrl
from .theme import COLORS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Local Plotly.js setup — copy once to data dir, serve via directory mode
# ---------------------------------------------------------------------------

_PLOTLY_JS_DIR: Path | None = None  # cached path to dir containing plotly.min.js


def _ensure_local_plotlyjs() -> Path | None:
    """Ensure plotly.min.js exists locally and return the directory Path."""
    global _PLOTLY_JS_DIR
    if _PLOTLY_JS_DIR is not None and (_PLOTLY_JS_DIR / "plotly.min.js").exists():
        return _PLOTLY_JS_DIR

    data_dir = Path.home() / ".tennis_analytics" / "data" / "plotly"
    data_dir.mkdir(parents=True, exist_ok=True)
    js_path = data_dir / "plotly.min.js"

    if not js_path.exists():
        src = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
        if src.exists():
            js_path.write_bytes(src.read_bytes())
            logger.info("Wrote local plotly.min.js (%d KB)", js_path.stat().st_size // 1024)
        else:
            logger.warning("plotly.min.js not found in package, falling back to CDN")
            return None

    _PLOTLY_JS_DIR = data_dir
    return _PLOTLY_JS_DIR


def get_chart_base_url() -> QUrl:
    """Return the QUrl base URL for loading chart HTML (so plotly.min.js resolves).
    
    All QWebEngineView.setHtml(html, base_url) calls should use this.
    """
    d = _ensure_local_plotlyjs()
    if d is not None:
        # Trailing slash is required so relative src="plotly.min.js" resolves
        return QUrl.fromLocalFile(str(d) + "/")
    return QUrl()

# Common Plotly layout settings for dark theme
_LAYOUT_DEFAULTS = dict(
    paper_bgcolor=COLORS["bg_primary"],
    plot_bgcolor=COLORS["bg_primary"],
    font=dict(family="Inter, Segoe UI, sans-serif", color=COLORS["text"], size=12),
    margin=dict(l=40, r=20, t=30, b=40),
    legend=dict(
        bgcolor="rgba(0,0,0,0)",
        font=dict(color=COLORS["text_dim"], size=11),
    ),
    xaxis=dict(
        gridcolor=COLORS["surface1"],
        zerolinecolor=COLORS["surface1"],
    ),
    yaxis=dict(
        gridcolor=COLORS["surface1"],
        zerolinecolor=COLORS["surface1"],
    ),
)


def _to_html(fig, height=350) -> str:
    """Convert a Plotly figure to a standalone HTML snippet."""
    fig.update_layout(**_LAYOUT_DEFAULTS)
    fig.update_layout(height=height)

    # Use local plotly.min.js via "directory" mode (small HTML, JS loaded by browser).
    # Callers must pass get_chart_base_url() to setHtml() so the relative
    # src="plotly.min.js" resolves correctly.
    plotly_dir = _ensure_local_plotlyjs()
    include_js = "directory" if plotly_dir else "cdn"

    html = fig.to_html(
        include_plotlyjs=include_js,
        full_html=True,
        config={"displayModeBar": False, "responsive": True, "scrollZoom": False},
    )

    # Inject CSS to remove body margins/scrollbars so chart fills QWebEngineView cleanly
    inject_css = (
        '<style>html,body{margin:0;padding:0;overflow:hidden;'
        f'background:{COLORS["bg_primary"]}}}</style>'
    )
    html = html.replace('</head>', f'{inject_css}</head>', 1)
    return html


# ---------------------------------------------------------------------------
# Spider / Radar chart
# ---------------------------------------------------------------------------

def spider_chart(categories: list[str],
                 values: list[float],
                 player_name: str = "",
                 max_val: float = 100,
                 height: int = 380) -> str:
    """
    Single-player radar chart.

    categories: axis labels (e.g., ['Serve', 'Return', 'Clutch', ...])
    values:     numeric score per category, 0-max_val.
    """
    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=values + [values[0]],  # close the polygon
        theta=categories + [categories[0]],
        fill="toself",
        fillcolor="rgba(0, 184, 212, 0.2)",
        line=dict(color=COLORS["accent"], width=2),
        name=player_name,
    ))
    fig.update_layout(
        polar=dict(
            bgcolor=COLORS["bg_primary"],
            radialaxis=dict(
                visible=True, range=[0, max_val],
                gridcolor=COLORS["surface1"],
                tickfont=dict(color=COLORS["text_muted"], size=9),
            ),
            angularaxis=dict(
                gridcolor=COLORS["surface1"],
                tickfont=dict(color=COLORS["text_dim"], size=11),
            ),
        ),
        showlegend=bool(player_name),
    )
    return _to_html(fig, height=height)


def spider_chart_dual(categories: list[str],
                      values1: list[float], name1: str,
                      values2: list[float], name2: str,
                      max_val: float = 100,
                      height: int = 400) -> str:
    """Overlapped radar chart for two players (H2H comparison)."""
    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=values1 + [values1[0]],
        theta=categories + [categories[0]],
        fill="toself",
        fillcolor="rgba(0, 184, 212, 0.2)",
        line=dict(color=COLORS["accent"], width=2),
        name=name1,
    ))
    fig.add_trace(go.Scatterpolar(
        r=values2 + [values2[0]],
        theta=categories + [categories[0]],
        fill="toself",
        fillcolor="rgba(239, 83, 80, 0.2)",
        line=dict(color=COLORS["red"], width=2),
        name=name2,
    ))
    fig.update_layout(
        polar=dict(
            bgcolor=COLORS["bg_primary"],
            radialaxis=dict(
                visible=True, range=[0, max_val],
                gridcolor=COLORS["surface1"],
                tickfont=dict(color=COLORS["text_muted"], size=9),
            ),
            angularaxis=dict(
                gridcolor=COLORS["surface1"],
                tickfont=dict(color=COLORS["text_dim"], size=11),
            ),
        ),
        showlegend=True,
    )
    return _to_html(fig, height=height)


# ---------------------------------------------------------------------------
# Bar chart — yearly W-L or H2H by surface
# ---------------------------------------------------------------------------

def bar_chart_yearly(yearly: dict, height: int = 300) -> str:
    """
    Year-by-year wins/losses bar chart.
    yearly: {year: {"wins": int, "losses": int}, ...}
    """
    years = sorted(yearly.keys())
    wins = [yearly[y]["wins"] for y in years]
    losses = [yearly[y]["losses"] for y in years]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=years, y=wins, name="Wins",
        marker_color=COLORS["green"],
    ))
    fig.add_trace(go.Bar(
        x=years, y=losses, name="Losses",
        marker_color=COLORS["red"],
    ))
    fig.update_layout(
        barmode="group",
        xaxis_title="Year",
        yaxis_title="Matches",
    )
    return _to_html(fig, height=height)


def bar_chart_h2h(surfaces: dict, p1_name: str, p2_name: str,
                  height: int = 280) -> str:
    """
    H2H by surface — grouped bars.
    surfaces: {surface_name: {"p1_wins": int, "p2_wins": int}, ...}
    """
    names = list(surfaces.keys())
    p1 = [surfaces[s]["p1_wins"] for s in names]
    p2 = [surfaces[s]["p2_wins"] for s in names]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=names, y=p1, name=p1_name.split()[-1],
        marker_color=COLORS["accent"],
    ))
    fig.add_trace(go.Bar(
        x=names, y=p2, name=p2_name.split()[-1],
        marker_color=COLORS["red"],
    ))
    fig.update_layout(barmode="group")
    return _to_html(fig, height=height)


# ---------------------------------------------------------------------------
# Line chart — yearly trends (serve/return/win %)
# ---------------------------------------------------------------------------

def line_chart_trends(yearly: dict, player_id, height: int = 300) -> str:
    """
    Yearly serve / return / win % trend lines.
    yearly: dict from compute_yearly_advanced_stats.
    """
    from ..core.stats_engine import compute_yearly_advanced_stats

    years = sorted(yearly.keys())
    serve = [yearly[y].get("avg_total_serve_won_pct", 0) for y in years]
    ret = [yearly[y].get("avg_return_pts_won_pct", 0) for y in years]
    win = [yearly[y].get("win_pct", 0) for y in years]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=years, y=serve, mode="lines+markers", name="Serve Pts Won %",
        line=dict(color=COLORS["green"], width=2),
        marker=dict(size=5),
    ))
    fig.add_trace(go.Scatter(
        x=years, y=ret, mode="lines+markers", name="Return Pts Won %",
        line=dict(color=COLORS["accent"], width=2),
        marker=dict(size=5),
    ))
    fig.add_trace(go.Scatter(
        x=years, y=win, mode="lines+markers", name="Win %",
        line=dict(color=COLORS["yellow"], width=2),
        marker=dict(size=5),
    ))
    fig.update_layout(
        yaxis_title="%",
        xaxis=dict(dtick=1),
    )
    return _to_html(fig, height=height)


# ---------------------------------------------------------------------------
# Pressure / Dominance chart
# ---------------------------------------------------------------------------

def pressure_chart(pressure: dict, height: int = 300) -> str:
    """
    Horizontal bar chart for pressure metrics.
    pressure: dict with keys like 'dominance_ratio', 'tiebreak_win_pct', etc.
    """
    labels = []
    values = []
    colors = []

    metrics = [
        ("Dominance Ratio", "dominance_ratio", COLORS["accent"]),
        ("BP Prevail", "breakpoints_prevail", COLORS["green"]),
        ("Deciding Set Win %", "deciding_set_win_pct", COLORS["yellow"]),
        ("Tiebreak Win %", "tiebreak_win_pct", COLORS["peach"]),
    ]
    for label, key, color in metrics:
        val = pressure.get(key)
        if val is not None:
            labels.append(label)
            values.append(float(val))
            colors.append(color)

    if not values:
        return ""

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=values, y=labels, orientation="h",
        marker_color=colors,
        text=[f"{v:.1f}" for v in values],
        textposition="auto",
        textfont=dict(color=COLORS["text"]),
    ))
    fig.update_layout(
        yaxis=dict(autorange="reversed"),
        xaxis_title="Value",
    )
    return _to_html(fig, height=height)


# ---------------------------------------------------------------------------
# Surface donut chart
# ---------------------------------------------------------------------------

def surface_donut(surfaces: dict, height: int = 280) -> str:
    """
    Donut chart of win percentages by surface.
    surfaces: {surface: {"wins": int, "losses": int}, ...}
    """
    labels = []
    values = []
    for surface, rec in surfaces.items():
        total = rec["wins"] + rec["losses"]
        if total > 0:
            labels.append(surface)
            values.append(total)

    if not values:
        return ""

    fig = go.Figure()
    fig.add_trace(go.Pie(
        labels=labels,
        values=values,
        hole=0.55,
        marker=dict(colors=[
            COLORS["accent"], COLORS["peach"],
            COLORS["green"], COLORS["mauve"],
        ]),
        textinfo="label+percent",
        textfont=dict(color=COLORS["text"]),
    ))
    fig.update_layout(showlegend=False)
    return _to_html(fig, height=height)


# ---------------------------------------------------------------------------
# Ranking history charts
# ---------------------------------------------------------------------------

def _aggregate_yearly_ranks(history: list) -> dict:
    """From a chronologically sorted ranking history, compute per-year
    best (lowest) rank and year-end rank.

    Returns:
        {year:int -> {"best": int, "year_end": int}}
    """
    by_year: dict = {}
    for row in history:
        d = str(row.get("ranking_date") or "")
        rk = row.get("rank")
        if rk is None or len(d) < 4:
            continue
        try:
            year = int(d[:4])
            rk = int(rk)
        except (TypeError, ValueError):
            continue
        rec = by_year.setdefault(year, {"best": rk, "year_end": rk,
                                        "_last_date": d})
        if rk < rec["best"]:
            rec["best"] = rk
        # history is already date-ordered, so the last assignment wins
        if d >= rec["_last_date"]:
            rec["year_end"] = rk
            rec["_last_date"] = d
    for rec in by_year.values():
        rec.pop("_last_date", None)
    return by_year


def rank_yearly_chart(history: list, height: int = 300) -> str:
    """Grouped bar chart: best rank vs year-end rank, per year.

    Y axis is reversed so rank #1 sits at the top.
    """
    by_year = _aggregate_yearly_ranks(history)
    if not by_year:
        return ""

    years = sorted(by_year.keys())
    best = [by_year[y]["best"] for y in years]
    year_end = [by_year[y]["year_end"] for y in years]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=years, y=best, name="Best of year",
        marker_color=COLORS["accent"],
        text=[str(v) for v in best],
        textposition="outside",
        textfont=dict(color=COLORS["text"], size=10),
        hovertemplate="<b>%{x}</b><br>Best: #%{y}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=years, y=year_end, name="Year-end",
        marker_color=COLORS["peach"],
        text=[str(v) for v in year_end],
        textposition="outside",
        textfont=dict(color=COLORS["text"], size=10),
        hovertemplate="<b>%{x}</b><br>Year-end: #%{y}<extra></extra>",
    ))
    fig.update_layout(
        barmode="group",
        xaxis=dict(title="Year", dtick=1),
        yaxis=dict(title="Rank", autorange="reversed", rangemode="tozero"),
    )
    return _to_html(fig, height=height)


def ranking_history_chart(history: list, height: int = 320) -> str:
    """Week-by-week ranking history line chart with career-high marker.

    `history` is a list of {"ranking_date": "YYYYMMDD", "rank": int, ...}
    sorted by date ascending.
    """
    if not history:
        return ""

    # Build (date, rank) series, skipping malformed rows
    xs: list = []
    ys: list = []
    for row in history:
        d = str(row.get("ranking_date") or "")
        rk = row.get("rank")
        if rk is None or len(d) != 8:
            continue
        try:
            iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            rk = int(rk)
        except (TypeError, ValueError):
            continue
        xs.append(iso)
        ys.append(rk)

    if not xs:
        return ""

    # Career-high = lowest rank (earliest occurrence)
    ch_idx = min(range(len(ys)), key=lambda i: ys[i])
    ch_date = xs[ch_idx]
    ch_rank = ys[ch_idx]
    # Pretty date for label
    try:
        y, m, d = ch_date.split("-")
        ch_label = f"Career High #{ch_rank} — {d}/{m}/{y}"
    except ValueError:
        ch_label = f"Career High #{ch_rank}"

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines", name="Rank",
        line=dict(color=COLORS["green"], width=1.6),
        hovertemplate="%{x}<br>Rank: #%{y}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[ch_date], y=[ch_rank],
        mode="markers+text",
        name="Career High",
        marker=dict(color=COLORS["red"], size=11,
                    line=dict(color=COLORS["text"], width=1)),
        text=[ch_label],
        textposition="bottom center",
        textfont=dict(color=COLORS["red"], size=11),
        hovertemplate=f"<b>{ch_label}</b><extra></extra>",
        showlegend=False,
    ))
    fig.update_layout(
        xaxis=dict(title="Date", type="date"),
        yaxis=dict(title="Rank", autorange="reversed", rangemode="tozero"),
        showlegend=False,
    )
    return _to_html(fig, height=height)

