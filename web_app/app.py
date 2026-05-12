"""Streamlit web front-end for TennisStatsV2.

This file is intentionally isolated from the PySide6 desktop app. It reuses
the existing read-only query layer through a Turso snapshot, so user clicks hit
local SQLite on the Streamlit server instead of making one Turso request per
query.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SNAPSHOT_TTL_SECONDS = int(os.environ.get("TENNIS_WEB_SNAPSHOT_TTL", "604800"))
WEB_SNAPSHOT_PAGE_SIZE = int(os.environ.get("TENNIS_WEB_SNAPSHOT_PAGE_SIZE", "5000"))
WEB_AUTO_REFRESH = os.environ.get("TENNIS_WEB_AUTO_REFRESH", "0").strip().lower() in {"1", "true", "yes"}
WEB_REMOTE_DIAGNOSTICS = os.environ.get("TENNIS_WEB_REMOTE_DIAGNOSTICS", "0").strip().lower() in {"1", "true", "yes"}
SNAPSHOT_ENGINE_VERSION = "web-snapshot-keyset-v5"
ENV_KEYS = ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "TURSO_LOCAL_PATH")
SNAPSHOT_REQUIRED_TABLES = ("players", "matches")
WEB_SNAPSHOT_TABLES = (
    "players", "matches", "rankings", "doubles_matches",
    "scrape_cache", "extended_stats_cache",
    "match_winners_errors", "match_serve_speed", "match_pbp_stats",
    "match_mcp_serve", "match_mcp_return", "match_mcp_rally",
    "match_mcp_tactics",
)

LEVEL_LABELS = {
    "G": "Grand Slam",
    "M": "Masters 1000 / WTA 1000",
    "A": "ATP 250/500",
    "P": "Premier / WTA",
    "D": "Davis/BJK Cup",
    "I": "International",
    "F": "Tour Finals",
    "E": "Elite Trophy",
}

GLOBAL_STATS = {
    "Most titles": "most_titles_overall",
    "Most finals": "most_finals_overall",
    "Career win percentage": "career_win_pct_overall",
    "Longest win streak": "win_streak_overall",
    "Most wins vs Top 10": "wins_vs_top10",
    "Weeks at No. 1 streak": "streak_weeks_at_no1",
    "Most bagels given": "most_bagels_given",
    "Deciding-set win percentage": "deciding_set_win_pct",
}

GLOBAL_LEVEL_OPTIONS = {
    "All": None,
    "Grand Slam": "Grand Slam",
    "Masters 1000": "Masters 1000",
    "Tour-level": "ATP 250",
    "ATP Finals": "ATP Finals",
    "Challenger": "Challenger",
}

EXTENDED_TABLES = {
    "Winners / Errors": ("match_winners_errors", "get_player_winners_errors"),
    "Serve Speed": ("match_serve_speed", "get_player_serve_speed"),
    "Point-by-Point": ("match_pbp_stats", "get_player_pbp_stats"),
    "MCP Serve": ("match_mcp_serve", "get_player_mcp_serve"),
    "MCP Return": ("match_mcp_return", "get_player_mcp_return"),
    "MCP Rally": ("match_mcp_rally", "get_player_mcp_rally"),
    "MCP Tactics": ("match_mcp_tactics", "get_player_mcp_tactics"),
}


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        clean_value = _clean_secret_value(key.strip(), value)
        if clean_value:
            os.environ.setdefault(key.strip(), clean_value)


def _clean_secret_value(key: str, value: Any) -> str:
    clean_value = str(value).strip().strip('"').strip("'").strip()
    if key == "TURSO_AUTH_TOKEN" and clean_value.lower().startswith("bearer "):
        clean_value = clean_value[7:].strip()
    return clean_value


def _apply_streamlit_secrets() -> None:
    """Expose local env files and Streamlit secrets as env vars consumed by cloud.db."""
    _load_env_file(ROOT / "web_app" / ".env")
    _load_env_file(ROOT / "cloud" / ".env")

    for key in ENV_KEYS:
        if os.environ.get(key):
            os.environ[key] = _clean_secret_value(key, os.environ[key])

    for key in ENV_KEYS:
        if os.environ.get(key):
            continue
        try:
            value = st.secrets.get(key)
        except Exception:
            value = None
        if value:
            os.environ[key] = _clean_secret_value(key, value)

    if not os.environ.get("TURSO_LOCAL_PATH"):
        cache_dir = Path(tempfile.gettempdir()) / "tennisstatsv2-web"
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ["TURSO_LOCAL_PATH"] = str(cache_dir / "tennis.db")


def _remove_local_snapshot() -> None:
    _apply_streamlit_secrets()
    from cloud.db import get_local_replica_path

    configured_path = get_local_replica_path()
    paths = {
        configured_path,
        configured_path.with_suffix(".tmp.db"),
        configured_path.parent / "tennis.db",
        configured_path.parent / "tennis.tmp.db",
    }
    for path in paths:
        if path.exists():
            path.unlink()


def _snapshot_counts(db) -> dict[str, int]:
    counts = {}
    for table in SNAPSHOT_REQUIRED_TABLES:
        row = db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        counts[table] = int(row[0] if row else 0)
    return counts


def _format_counts(counts: dict[str, int]) -> str:
    parts = []
    for table in SNAPSHOT_REQUIRED_TABLES:
        count = counts.get(table, -1)
        value = "missing" if count < 0 else f"{count:,}"
        parts.append(f"{table}={value}")
    return ", ".join(parts)


def _format_table_counts(counts: dict[str, int], tables: tuple[str, ...] = SNAPSHOT_REQUIRED_TABLES) -> str:
    return ", ".join(f"{table}={counts.get(table, 0):,}" for table in tables)


def _remote_snapshot_counts() -> dict[str, int]:
    _apply_streamlit_secrets()
    import libsql_client
    from cloud.db import _auth_token, _http_url

    client = libsql_client.create_client_sync(url=_http_url(), auth_token=_auth_token())
    try:
        counts = {}
        for table in SNAPSHOT_REQUIRED_TABLES:
            try:
                result = client.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = int(result.rows[0][0]) if result.rows else 0
            except Exception as exc:
                if "no such table" in str(exc).lower():
                    counts[table] = -1
                    continue
                raise
        return counts
    finally:
        client.close()


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        counts = {}
        for table in SNAPSHOT_REQUIRED_TABLES:
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()
                counts[table] = int(row[0] if row else 0)
            except sqlite3.DatabaseError:
                counts[table] = -1
        return counts
    finally:
        conn.close()


def _local_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _infer_sqlite_type(values: list[Any]) -> str:
    non_null_values = [value for value in values if value is not None]
    if not non_null_values:
        return "TEXT"
    if all(isinstance(value, bool | int) and not isinstance(value, bool) for value in non_null_values):
        return "INTEGER"
    if all(isinstance(value, bool | int | float) and not isinstance(value, bool) for value in non_null_values):
        return "REAL"
    return "TEXT"


def _create_local_table_from_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    rows: list[Any],
) -> None:
    definitions = []
    for index, column in enumerate(columns):
        sample_values = [row[index] for row in rows]
        definitions.append(f"{_quote_identifier(column)} {_infer_sqlite_type(sample_values)}")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_quote_identifier(table)} ({', '.join(definitions)})"
    )


def _download_web_snapshot(dest: Path) -> Path:
    _apply_streamlit_secrets()
    import libsql_client
    from cloud.db import _auth_token, _http_url

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.db")
    if tmp.exists():
        tmp.unlink()

    client = libsql_client.create_client_sync(url=_http_url(), auth_token=_auth_token())
    local = None
    success = False
    try:
        local = sqlite3.connect(str(tmp))
        local.execute("PRAGMA journal_mode=OFF")
        local.execute("PRAGMA synchronous=OFF")

        copied_by_table = {}
        for table in WEB_SNAPSHOT_TABLES:
            quoted_table = _quote_identifier(table)
            last_rowid = 0
            copied = 0
            created = False
            while True:
                try:
                    result = client.execute(
                        f"SELECT rowid AS __snapshot_rowid, * FROM {quoted_table} "
                        f"WHERE rowid > {last_rowid} ORDER BY rowid LIMIT {WEB_SNAPSHOT_PAGE_SIZE}"
                    )
                except Exception as exc:
                    message = str(exc).lower()
                    if "no such table" in message and table not in SNAPSHOT_REQUIRED_TABLES:
                        copied_by_table[table] = 0
                        break
                    raise RuntimeError(f"Turso query failed while copying table {table}: {exc}") from exc
                rows = list(result.rows or [])
                raw_columns = [str(column) for column in list(result.columns or [])]
                if not raw_columns:
                    if rows:
                        raise RuntimeError(f"Remote query for {table} returned rows but no column metadata")
                    break
                columns = raw_columns[1:]
                if not created:
                    _create_local_table_from_rows(local, table, columns, [row[1:] for row in rows])
                    created = True
                if not rows:
                    break
                column_list = ", ".join(_quote_identifier(column) for column in columns)
                placeholders = ", ".join("?" for _ in columns)
                local.executemany(
                    f"INSERT INTO {quoted_table} ({column_list}) VALUES ({placeholders})",
                    [tuple(row[1:]) for row in rows],
                )
                copied += len(rows)
                last_rowid = int(rows[-1][0])
                if len(rows) < WEB_SNAPSHOT_PAGE_SIZE:
                    break
            if table in SNAPSHOT_REQUIRED_TABLES and copied <= 0:
                raise RuntimeError(
                    f"Copied 0 rows for required table {table}; created={created}."
                )
            copied_by_table[table] = copied

        local.commit()
        local.close()
        local = None

        copied_counts = _sqlite_counts(tmp)
        if any(copied_counts.get(table, 0) <= 0 for table in SNAPSHOT_REQUIRED_TABLES):
            raise RuntimeError(
                f"{SNAPSHOT_ENGINE_VERSION}: downloaded snapshot is empty after copy "
                f"({_format_counts(copied_counts)}); copied rows were "
                f"{_format_table_counts(copied_by_table)}."
            )

        if dest.exists():
            dest.unlink()
        tmp.replace(dest)
        success = True
        return dest
    finally:
        if local is not None:
            local.close()
        client.close()
        if not success and tmp.exists():
            tmp.unlink()


class WebSnapshotDatabase:
    def __init__(self, path: Path):
        from tennis_app.core.database import TennisDatabase

        self._impl = object.__new__(TennisDatabase)
        self._impl.db_path = path
        self._impl.conn = sqlite3.connect(str(path), check_same_thread=False)
        self._impl.conn.row_factory = sqlite3.Row
        self._impl._write_lock = threading.RLock()
        self._impl._career_stats_cache = {}
        self._impl._ranking_history_cache = {}
        self._impl.conn.execute("PRAGMA synchronous=NORMAL")
        self._impl.conn.execute("PRAGMA cache_size=-64000")
        self._impl.conn.execute("PRAGMA temp_store=MEMORY")
        self._impl.conn.execute("PRAGMA mmap_size=268435456")
        self._impl.conn.execute("PRAGMA busy_timeout=10000")

    def __getattr__(self, item):
        return getattr(self._impl, item)

    def close(self) -> None:
        self._impl.close()


def _validate_snapshot(db) -> dict[str, int]:
    counts = _snapshot_counts(db)
    empty_tables = [table for table, count in counts.items() if count <= 0]
    if empty_tables:
        raise RuntimeError(
            "Local snapshot contains no usable tennis data "
            f"({_format_counts(counts)}). Rebuild the snapshot after checking Turso secrets."
        )
    return counts


def _empty_snapshot_error(exc: Exception) -> RuntimeError:
    if not WEB_REMOTE_DIAGNOSTICS:
        return RuntimeError(
            f"{exc}\nRemote row-count diagnostics are disabled to avoid extra Turso reads."
        )
    try:
        remote_counts = _remote_snapshot_counts()
    except Exception as remote_exc:
        return RuntimeError(
            f"{exc}\nRemote Turso preflight also failed: {remote_exc}"
        )

    return RuntimeError(
        f"{exc}\nRemote Turso counts: {_format_counts(remote_counts)}. "
        "If these counts are zero or missing, Streamlit Cloud is connected to an empty/wrong Turso database."
    )


def _snapshot_is_stale(path: Path) -> bool:
    if not path.exists():
        return True
    if not WEB_AUTO_REFRESH:
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds >= SNAPSHOT_TTL_SECONDS


def show_connection_error(exc: Exception) -> None:
    st.error("Turso connection is not configured or the snapshot could not be loaded.")
    st.caption(f"Snapshot engine: {SNAPSHOT_ENGINE_VERSION}")
    st.code(str(exc))
    message = str(exc).lower()
    missing = [key for key in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN") if not os.environ.get(key)]
    if missing:
        st.info(
            "Missing configuration: "
            + ", ".join(missing)
            + ". Locally, add them to cloud/.env or web_app/.env. On Streamlit Cloud, add them in App settings > Secrets."
        )
        st.code(
            'TURSO_DATABASE_URL = "libsql://..."\nTURSO_AUTH_TOKEN = "..."',
            language="toml",
        )
    elif "401" in message or "unauthorized" in message:
        st.info(
            "Turso returned 401, so the URL is reachable but the auth token was rejected. "
            "Regenerate a database token for the same Turso database used in TURSO_DATABASE_URL, "
            "paste only the raw token value in Streamlit secrets, then reboot the Streamlit app."
        )
    elif "remote turso counts" in message and ("=0" in message or "missing" in message):
        st.info(
            "The Turso preflight ran. If the remote counts shown above are non-zero, Turso is fine and the local snapshot copy/open failed. "
            "Deploy the latest web_app/app.py, reboot, then use Rebuild snapshot. If the remote counts are zero or missing, check TURSO_DATABASE_URL."
        )


@st.cache_resource(ttl=SNAPSHOT_TTL_SECONDS, show_spinner="Refreshing local data snapshot...")
def get_db(refresh_token: int = 0, snapshot_action: str = "auto"):
    _apply_streamlit_secrets()
    from cloud.db import get_local_replica_path

    if snapshot_action == "rebuild":
        _remove_local_snapshot()

    path = get_local_replica_path()
    refresh_on_open = snapshot_action in {"refresh", "rebuild"} or _snapshot_is_stale(path)
    if refresh_on_open:
        _download_web_snapshot(path)

    db = WebSnapshotDatabase(path)
    try:
        _validate_snapshot(db)
    except Exception:
        try:
            db.close()
        except Exception:
            pass
        _remove_local_snapshot()
        _download_web_snapshot(path)
        db = WebSnapshotDatabase(path)
        try:
            _validate_snapshot(db)
        except Exception as exc:
            raise _empty_snapshot_error(exc) from exc
    return db


def run_query(db, sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    cur = db.conn.execute(sql, params)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    return pd.DataFrame([tuple(row) for row in rows], columns=cols)


def rows_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def format_date(value: Any) -> str:
    text = str(value or "")
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def player_name(player: dict[str, Any] | None) -> str:
    if not player:
        return ""
    return f"{player.get('name_first') or ''} {player.get('name_last') or ''}".strip()


def clean_player_name_for_db(name: str) -> str:
    try:
        from tennis_app.core.scraper import clean_player_name

        return clean_player_name(name) or name
    except Exception:
        return name


def pick_player(db, label: str, key: str, default: str = "") -> dict[str, Any] | None:
    query = st.text_input(label, value=default, key=f"{key}_query")
    if len(query.strip()) < 2:
        return None
    players = db.search_players(query.strip(), limit=25)
    if not players:
        st.warning("No players found.")
        return None

    options = {
        f"{player_name(p)} ({p.get('tour', '').upper()})": p for p in players
    }
    selected = st.selectbox("Select player", list(options), key=f"{key}_select")
    return options[selected]


def add_download(df: pd.DataFrame, name: str) -> None:
    if df.empty:
        return
    st.download_button(
        "Download CSV",
        df.to_csv(index=False).encode("utf-8"),
        file_name=name,
        mime="text/csv",
        width="stretch",
    )


def display_matches(df: pd.DataFrame, limit: int | None = None) -> None:
    if df.empty:
        st.info("No matches found.")
        return
    show = df.copy()
    if limit:
        show = show.head(limit)
    columns = [
        "tourney_date", "tourney_name", "surface", "tourney_level", "round",
        "winner_name", "loser_name", "score", "winner_rank", "loser_rank", "tour",
    ]
    columns = [c for c in columns if c in show.columns]
    show = show[columns]
    if "tourney_date" in show.columns:
        show["tourney_date"] = show["tourney_date"].map(format_date)
    if "tourney_level" in show.columns:
        show["tourney_level"] = show["tourney_level"].map(lambda x: LEVEL_LABELS.get(x, x))
    st.dataframe(show, hide_index=True, width="stretch")


def render_snapshot_status(db) -> None:
    try:
        from cloud.db import get_local_replica_path

        path = get_local_replica_path()
        size_mb = path.stat().st_size / (1024 * 1024) if path.exists() else 0
        counts = _snapshot_counts(db)
    except Exception as exc:
        with st.expander("Snapshot status"):
            st.warning(f"Snapshot status unavailable: {exc}")
        return

    with st.expander("Snapshot status"):
        cols = st.columns(3)
        cols[0].metric("Snapshot file", f"{size_mb:.1f} MB")
        cols[1].metric("Players", f"{counts.get('players', 0):,}")
        cols[2].metric("Matches", f"{counts.get('matches', 0):,}")
        st.caption(f"Local snapshot path: {path}")
        st.caption(f"Snapshot engine: {SNAPSHOT_ENGINE_VERSION}")


def render_global_dataset_coverage(db) -> None:
    coverage = run_query(
        db,
        """
        SELECT
          COUNT(*) AS total_matches,
          SUM(CASE WHEN tourney_id = 'SCRAPED' THEN 1 ELSE 0 END) AS scraped_matches,
          SUM(CASE WHEN tourney_id != 'SCRAPED' THEN 1 ELSE 0 END) AS historical_matches,
          MIN(SUBSTR(tourney_date, 1, 4)) AS first_year,
          MAX(SUBSTR(tourney_date, 1, 4)) AS last_year
        FROM matches
        WHERE is_upcoming = 0 OR is_upcoming IS NULL
        """,
    )
    if coverage.empty:
        return
    row = coverage.iloc[0]
    total = int(row.total_matches or 0)
    scraped = int(row.scraped_matches or 0)
    historical = int(row.historical_matches or 0)
    years = f"{row.first_year or '-'}-{row.last_year or '-'}"
    if total and historical == 0:
        st.warning(
            "Global leaderboards are currently based on the live scraped cloud dataset only "
            f"({scraped:,} matches, {years}). Historical CSV matches have not been imported into Turso yet, "
            "so all-time records may be incomplete."
        )
    else:
        st.caption(
            f"Dataset coverage: {total:,} matches ({historical:,} historical CSV, {scraped:,} scraped), {years}."
        )


def render_header() -> None:
    st.set_page_config(page_title="TennisStatsV2 Web", layout="wide")
    st.markdown(
        """
        <style>
        :root { --accent: #0f766e; --ink: #17202a; --muted: #5b6472; }
        .block-container { padding-top: 1.4rem; padding-bottom: 2rem; }
        h1, h2, h3 { letter-spacing: 0; color: var(--ink); }
        div[data-testid="stMetric"] {
            background: #ffffff; border: 1px solid #d8dee8; border-radius: 8px;
            padding: 0.7rem 0.9rem; box-shadow: 0 1px 2px rgba(23,32,42,0.04);
        }
        div[data-testid="stTabs"] button { font-weight: 650; }
        .stButton > button, .stDownloadButton > button {
            border-radius: 8px; border-color: #b9c3d0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    left, right = st.columns([0.64, 0.36], vertical_alignment="center")
    with left:
        st.title("TennisStatsV2")
    with right:
        allow_download = st.checkbox("Allow Turso download", value=False)
        refresh_col, rebuild_col = st.columns(2)
        if refresh_col.button("Reload local", width="stretch"):
            get_db.clear()
            st.session_state["snapshot_action"] = "auto"
            st.session_state["refresh_token"] = st.session_state.get("refresh_token", 0) + 1
            st.rerun()
        if rebuild_col.button("Rebuild from Turso", width="stretch", disabled=not allow_download):
            get_db.clear()
            st.session_state["snapshot_action"] = "rebuild"
            st.session_state["refresh_token"] = st.session_state.get("refresh_token", 0) + 1
            st.rerun()


def page_dashboard(db) -> None:
    counts = run_query(
        db,
        """
        SELECT
          (SELECT COUNT(*) FROM players) AS players,
          (SELECT COUNT(*) FROM matches WHERE is_upcoming = 0 OR is_upcoming IS NULL) AS matches,
          (SELECT COUNT(*) FROM matches WHERE tourney_id = 'SCRAPED') AS scraped_matches,
          (SELECT COUNT(*) FROM rankings WHERE ranking_date LIKE 'SCRAPED_%') AS ranking_rows,
          (SELECT COUNT(*) FROM match_winners_errors) AS extended_rows
        """,
    )
    if not counts.empty:
        row = counts.iloc[0]
        cols = st.columns(5)
        cols[0].metric("Players", f"{int(row.players):,}")
        cols[1].metric("Matches", f"{int(row.matches):,}")
        cols[2].metric("Scraped", f"{int(row.scraped_matches):,}")
        cols[3].metric("Rankings", f"{int(row.ranking_rows):,}")
        cols[4].metric("Ext rows", f"{int(row.extended_rows):,}")

    st.subheader("Latest completed matches")
    latest = run_query(
        db,
        """
        SELECT tourney_date, tourney_name, surface, tourney_level, round,
               winner_name, loser_name, score, winner_rank, loser_rank, tour
        FROM matches
        WHERE is_upcoming = 0 OR is_upcoming IS NULL
        ORDER BY tourney_date DESC, match_num DESC
        LIMIT 40
        """,
    )
    display_matches(latest)


def page_rankings(db) -> None:
    controls = st.columns([0.16, 0.16, 0.2, 0.48])
    tour = controls[0].segmented_control("Tour", ["atp", "wta"], default="atp", format_func=str.upper)
    top_n = controls[1].number_input("Top", min_value=10, max_value=1000, value=100, step=10)
    marker_rows = run_query(
        db,
        "SELECT DISTINCT ranking_date FROM rankings WHERE tour = ? ORDER BY ranking_date DESC LIMIT 30",
        (tour,),
    )
    dates = marker_rows["ranking_date"].tolist() if not marker_rows.empty else []
    preferred = ["SCRAPED_OFFICIAL_SINGLES", "SCRAPED_LIVE_SINGLES", "LIVE"]
    default_date = next((date for date in preferred if date in dates), dates[0] if dates else None)
    if not default_date:
        st.info("No ranking rows found.")
        return
    selected_date = controls[2].selectbox(
        "Snapshot",
        dates,
        index=dates.index(default_date),
    )

    rankings, ranking_date = db.get_rankings(tour=tour, date=selected_date, top_n=int(top_n))
    df = rows_to_frame(rankings)
    controls[3].metric("Ranking date", ranking_date or "-")
    if df.empty:
        st.info("No ranking rows found.")
        return

    df["player"] = (df["name_first"].fillna("") + " " + df["name_last"].fillna("")).str.strip()
    show = df[["rank", "player", "points", "age", "rank_diff", "pts_diff", "next_tournament", "ioc", "tour"]]
    st.dataframe(show, hide_index=True, width="stretch")
    add_download(show, f"rankings_{tour}_{ranking_date}.csv")


def page_player(db) -> None:
    player = pick_player(db, "Search player", "player_page")
    if not player:
        return
    name = player_name(player)
    tour = player.get("tour") or "atp"
    stats = db.get_player_career_stats(player["player_id"], tour=tour)
    streaks = db.get_player_streaks(player["player_id"], tour=tour)
    upcoming = db.get_player_upcoming_match(name)

    st.subheader(f"{name} ({tour.upper()})")
    cols = st.columns(5)
    wins = int(stats.get("wins", 0) or 0)
    losses = int(stats.get("losses", 0) or 0)
    total = wins + losses
    cols[0].metric("Record", f"{wins}-{losses}")
    cols[1].metric("Win %", f"{wins / total * 100:.1f}%" if total else "-")
    cols[2].metric("Titles", int(stats.get("titles", 0) or 0))
    cols[3].metric("Best streak", int(streaks.get("best_win_streak", 0) or 0))
    cols[4].metric("Current streak", int(streaks.get("current_win_streak", 0) or 0))

    if upcoming:
        st.info(
            f"Upcoming: {upcoming.get('tourney_name', '')} vs "
            f"{upcoming.get('opponent') or 'TBD'} on {format_date(upcoming.get('tourney_date'))}"
        )

    chart_cols = st.columns([0.42, 0.58])
    surface_rows = []
    for surface, values in (stats.get("surfaces") or {}).items():
        surface_rows.append({"surface": surface, "wins": values.get("wins", 0), "losses": values.get("losses", 0)})
    if surface_rows:
        surface_df = pd.DataFrame(surface_rows)
        surface_df["matches"] = surface_df["wins"] + surface_df["losses"]
        fig = px.bar(surface_df, x="surface", y=["wins", "losses"], barmode="stack", color_discrete_sequence=["#0f766e", "#c2410c"])
        chart_cols[0].plotly_chart(fig, width="stretch")

    rank_history = rows_to_frame(db.get_player_ranking_history(player["player_id"], tour))
    if not rank_history.empty:
        rank_history["date"] = pd.to_datetime(rank_history["ranking_date"], format="%Y%m%d", errors="coerce")
        fig = px.line(rank_history.dropna(subset=["date"]), x="date", y="rank", markers=False)
        fig.update_yaxes(autorange="reversed")
        chart_cols[1].plotly_chart(fig, width="stretch")

    matches = rows_to_frame(db.get_player_matches(player["player_id"], tour=tour))
    st.subheader("Matches")
    display_matches(matches, limit=100)
    add_download(matches, f"matches_{name.replace(' ', '_')}.csv")


def page_matches(db) -> None:
    controls = st.columns(5)
    tour = controls[0].segmented_control("Tour", ["atp", "wta"], default="atp", format_func=str.upper, key="matches_tour")
    year = controls[1].number_input("Year", min_value=1968, max_value=datetime.now().year + 1, value=datetime.now().year, step=1)
    surface = controls[2].selectbox("Surface", [None, "Hard", "Clay", "Grass", "Carpet"], format_func=lambda x: "All" if x is None else x)
    level = controls[3].selectbox("Level", [None] + list(LEVEL_LABELS), format_func=lambda x: "All" if x is None else LEVEL_LABELS.get(x, x))
    text = controls[4].text_input("Player / tournament")

    params: list[Any] = [tour, f"{int(year)}0000", f"{int(year)}9999"]
    filters = ["tour = ?", "tourney_date BETWEEN ? AND ?", "(is_upcoming = 0 OR is_upcoming IS NULL)"]
    if surface:
        filters.append("surface = ?")
        params.append(surface)
    if level:
        filters.append("tourney_level = ?")
        params.append(level)
    if text.strip():
        filters.append("(winner_name LIKE ? OR loser_name LIKE ? OR tourney_name LIKE ?)")
        like = f"%{text.strip()}%"
        params.extend([like, like, like])
    where = " AND ".join(filters)
    df = run_query(
        db,
        f"""
        SELECT tourney_date, tourney_name, surface, tourney_level, round,
               winner_name, loser_name, score, winner_rank, loser_rank, tour
        FROM matches
        WHERE {where}
        ORDER BY tourney_date DESC, match_num DESC
        LIMIT 1000
        """,
        tuple(params),
    )
    display_matches(df)
    add_download(df, f"matches_{tour}_{int(year)}.csv")


def page_h2h(db) -> None:
    col1, col2 = st.columns(2)
    with col1:
        p1 = pick_player(db, "Player 1", "h2h_p1")
    with col2:
        p2 = pick_player(db, "Player 2", "h2h_p2")
    if not p1 or not p2:
        return
    if (p1.get("tour") or "") != (p2.get("tour") or ""):
        st.warning("Players are on different tours.")
        return
    result = db.get_head_to_head(p1["player_id"], p2["player_id"], tour=p1.get("tour"))
    c1, c2, c3 = st.columns(3)
    c1.metric(player_name(p1), result.get("p1_wins", 0))
    c2.metric("Matches", result.get("total_matches", 0))
    c3.metric(player_name(p2), result.get("p2_wins", 0))
    surface = result.get("by_surface") or {}
    if surface:
        surf_df = pd.DataFrame([
            {"surface": k, player_name(p1): v.get("p1_wins", 0), player_name(p2): v.get("p2_wins", 0)}
            for k, v in surface.items()
        ])
        fig = px.bar(surf_df, x="surface", y=[player_name(p1), player_name(p2)], barmode="group", color_discrete_sequence=["#0f766e", "#c2410c"])
        st.plotly_chart(fig, width="stretch")
    display_matches(rows_to_frame(result.get("matches") or []))


def page_tournaments(db) -> None:
    controls = st.columns([0.2, 0.2, 0.6])
    tour = controls[0].segmented_control("Tour", ["atp", "wta"], default="atp", format_func=str.upper, key="tournament_tour")
    year = controls[1].number_input("Year", min_value=1968, max_value=datetime.now().year + 1, value=datetime.now().year, step=1, key="tournament_year")
    query = controls[2].text_input("Tournament contains")

    tournaments = rows_to_frame(db.get_tournament_list(year=int(year), tour=tour))
    if query.strip() and not tournaments.empty:
        tournaments = tournaments[tournaments["tourney_name"].str.contains(query.strip(), case=False, na=False)]
    if tournaments.empty:
        st.info("No tournaments found.")
        return
    tournaments["label"] = tournaments["tourney_name"] + " - " + tournaments["tourney_date"].map(format_date)
    selected = st.selectbox("Tournament", tournaments["label"].tolist())
    selected_row = tournaments[tournaments["label"] == selected].iloc[0]
    results = run_query(
        db,
        """
        SELECT * FROM matches
        WHERE tour = ? AND tourney_name = ?
          AND tourney_date BETWEEN ? AND ?
          AND (is_upcoming = 0 OR is_upcoming IS NULL)
        ORDER BY tourney_date DESC, match_num DESC
        """,
        (tour, selected_row["tourney_name"], f"{int(year)}0000", f"{int(year)}9999"),
    )
    display_matches(results)


def page_extended(db) -> None:
    player = pick_player(db, "Search player", "extended_player")
    if not player:
        return
    name = clean_player_name_for_db(player_name(player))
    counts = db.get_extended_stats_count(name)
    cols = st.columns(len(EXTENDED_TABLES))
    for idx, (label, (table, _method)) in enumerate(EXTENDED_TABLES.items()):
        cols[idx].metric(label.split()[0], counts.get(table, 0))

    table_label = st.selectbox("Table", list(EXTENDED_TABLES))
    surface = st.selectbox("Surface", [None, "Hard", "Clay", "Grass", "Carpet"], key="ext_surface", format_func=lambda x: "All" if x is None else x)
    year = st.number_input("Year", min_value=0, max_value=datetime.now().year + 1, value=0, step=1, key="ext_year")
    _table, method_name = EXTENDED_TABLES[table_label]
    method = getattr(db, method_name)
    rows = method(name, surface=surface, year=int(year) if year else None)
    df = rows_to_frame(rows)
    if df.empty:
        st.info("No extended rows found for this selection.")
        return
    if "tourney_date" in df.columns:
        df["tourney_date"] = df["tourney_date"].map(format_date)
    st.dataframe(df, hide_index=True, width="stretch")
    add_download(df, f"extended_{name.replace(' ', '_')}_{_table}.csv")


def page_global(db) -> None:
    from tennis_app.core.global_stats_engine import GlobalStatsEngine

    controls = st.columns(5)
    stat_name = controls[0].selectbox("Leaderboard", list(GLOBAL_STATS))
    tour = controls[1].segmented_control("Tour", ["atp", "wta"], default="atp", format_func=str.upper, key="global_tour")
    surface = controls[2].selectbox("Surface", [None, "Hard", "Clay", "Grass", "Carpet"], format_func=lambda x: "All" if x is None else x, key="global_surface")
    level_label = controls[3].selectbox("Level", list(GLOBAL_LEVEL_OPTIONS), key="global_level")
    limit = controls[4].number_input("Limit", min_value=10, max_value=100, value=50, step=10)

    current_year = datetime.now().year
    year_range = st.slider("Year range", 1990, current_year, (1990, current_year), key="global_year_range")
    min_year = year_range[0] if year_range != (1990, current_year) else None
    max_year = year_range[1] if year_range != (1990, current_year) else None

    render_global_dataset_coverage(db)

    filters = {"tour": tour, "surface": surface, "level": GLOBAL_LEVEL_OPTIONS[level_label], "min_matches": 10, "min_year": min_year, "max_year": max_year}
    result = GlobalStatsEngine(db).compute(GLOBAL_STATS[stat_name], filters, limit=int(limit))
    rows = result.get("rows") or []
    if rows and isinstance(rows[0], dict):
        df = pd.DataFrame(rows)
    else:
        df = pd.DataFrame(rows, columns=result.get("columns"))
    if df.empty:
        st.info(result.get("message") or "No rows found.")
        return
    st.dataframe(df, hide_index=True, width="stretch")
    numeric_cols = []
    for col in df.columns:
        converted = pd.to_numeric(df[col], errors="coerce")
        if converted.notna().any():
            df[f"_{col}_numeric"] = converted
            numeric_cols.append(f"_{col}_numeric")
    name_col = next(
        (c for c in ("Player", "player", "name", "winner_name", "label") if c in df.columns),
        df.columns[0],
    )
    if numeric_cols:
        value_col = numeric_cols[-1]
        fig = px.bar(df.head(20), x=value_col, y=name_col, orientation="h", color_discrete_sequence=["#0f766e"])
        value_label = value_col[1:-8] if value_col.startswith("_") and value_col.endswith("_numeric") else value_col
        fig.update_layout(xaxis_title=value_label, yaxis_title=name_col)
        fig.update_yaxes(autorange="reversed")
        st.plotly_chart(fig, width="stretch")


def main() -> None:
    render_header()
    refresh_token = st.session_state.get("refresh_token", 0)
    snapshot_action = st.session_state.pop("snapshot_action", "auto")
    try:
        db = get_db(refresh_token, snapshot_action)
    except Exception as exc:
        show_connection_error(exc)
        st.stop()

    render_snapshot_status(db)

    page = st.segmented_control(
        "View",
        ["Dashboard", "Rankings", "Player", "Matches", "H2H", "Tournaments", "Extended", "Global"],
        default="Dashboard",
        label_visibility="collapsed",
    )
    if page == "Dashboard":
        page_dashboard(db)
    elif page == "Rankings":
        page_rankings(db)
    elif page == "Player":
        page_player(db)
    elif page == "Matches":
        page_matches(db)
    elif page == "H2H":
        page_h2h(db)
    elif page == "Tournaments":
        page_tournaments(db)
    elif page == "Extended":
        page_extended(db)
    elif page == "Global":
        page_global(db)


if __name__ == "__main__":
    main()
