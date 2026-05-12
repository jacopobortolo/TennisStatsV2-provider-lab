"""
Data manager for downloading and caching tennis CSV data
from Jeff Sackmann's GitHub repositories, plus live scraping
from tennisabstract.com for recent data.
"""

import datetime
import hashlib
import os
import re
import time
import logging
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import pandas as pd

from .scraper import (
    TennisAbstractAccessError,
    TennisAbstractScraper,
    clean_player_name,
)
from .match_providers import MATCH_PROVIDER_ENV, get_match_provider

logger = logging.getLogger(__name__)

BASE_URL_ATP = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master"
BASE_URL_WTA = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master"

CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600  # 1 week


def get_data_dir():
    """Return the path to the local data cache directory."""
    data_dir = Path.home() / ".tennis_analytics" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _download_file(url, dest_path):
    """Download a file from a URL to a local path."""
    logger.info("Downloading %s", url)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(resp.content)
    logger.info("Saved %s (%d bytes)", dest_path.name, len(resp.content))


def _is_fresh(path):
    """Check if a cached file is still fresh."""
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_MAX_AGE_SECONDS


def _parallel_download(files_to_download, data_dir, force=False,
                       progress_callback=None):
    """Download files in parallel using a thread pool."""
    total = len(files_to_download)
    to_fetch = []
    for fname, url in files_to_download:
        dest = data_dir / fname
        if force or not _is_fresh(dest):
            to_fetch.append((fname, url, dest))
        else:
            logger.debug("Using cached %s", fname)

    done = total - len(to_fetch)
    if progress_callback:
        progress_callback(done, total, "Downloading...")

    def _do_download(item):
        fname, url, dest = item
        try:
            _download_file(url, dest)
        except requests.HTTPError as exc:
            logger.warning("Could not download %s: %s", fname, exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_do_download, item): item for item in to_fetch}
        for future in as_completed(futures):
            done += 1
            if progress_callback:
                fname = futures[future][0]
                progress_callback(done, total, f"Downloaded {fname}...")

    if progress_callback:
        progress_callback(total, total, "Download complete!")


def download_atp_data(year_start=1968, year_end=None, force=False,
                      progress_callback=None):
    """
    Download ATP match data, player list, and rankings from GitHub.

    Parameters
    ----------
    year_start : int
        First year of match data to download.
    year_end : int
        Last year of match data to download (inclusive).
    force : bool
        If True, re-download even if cache is fresh.
    progress_callback : callable, optional
        Called with (current_step, total_steps, message) for progress updates.
    """
    if year_end is None:
        year_end = datetime.date.today().year
    data_dir = get_data_dir() / "atp"
    data_dir.mkdir(parents=True, exist_ok=True)

    files_to_download = []

    # Players file
    files_to_download.append(("atp_players.csv", f"{BASE_URL_ATP}/atp_players.csv"))

    # Rankings — current + historical decades
    files_to_download.append((
        "atp_rankings_current.csv",
        f"{BASE_URL_ATP}/atp_rankings_current.csv",
    ))
    for decade in ("70s", "80s", "90s", "00s", "10s", "20s"):
        fname = f"atp_rankings_{decade}.csv"
        files_to_download.append((fname, f"{BASE_URL_ATP}/{fname}"))

    # Tour-level match files per year
    for year in range(year_start, year_end + 1):
        fname = f"atp_matches_{year}.csv"
        files_to_download.append((fname, f"{BASE_URL_ATP}/{fname}"))

    # Qualifying / challenger match files (available from 1978)
    for year in range(max(year_start, 1978), year_end + 1):
        fname = f"atp_matches_qual_chall_{year}.csv"
        files_to_download.append((fname, f"{BASE_URL_ATP}/{fname}"))

    # Futures match files (available from 1991)
    for year in range(max(year_start, 1991), year_end + 1):
        fname = f"atp_matches_futures_{year}.csv"
        files_to_download.append((fname, f"{BASE_URL_ATP}/{fname}"))

    # Doubles match files (available 2000–2020)
    for year in range(max(year_start, 2000), min(year_end, 2020) + 1):
        fname = f"atp_matches_doubles_{year}.csv"
        files_to_download.append((fname, f"{BASE_URL_ATP}/{fname}"))

    _parallel_download(files_to_download, data_dir, force, progress_callback)


def download_wta_data(year_start=1968, year_end=None, force=False,
                      progress_callback=None):
    """Download WTA match data, player list, and rankings from GitHub."""
    if year_end is None:
        year_end = datetime.date.today().year
    data_dir = get_data_dir() / "wta"
    data_dir.mkdir(parents=True, exist_ok=True)

    files_to_download = []
    files_to_download.append(("wta_players.csv", f"{BASE_URL_WTA}/wta_players.csv"))

    # Rankings — current + historical decades
    files_to_download.append((
        "wta_rankings_current.csv",
        f"{BASE_URL_WTA}/wta_rankings_current.csv",
    ))
    for decade in ("80s", "90s", "00s", "10s", "20s"):
        fname = f"wta_rankings_{decade}.csv"
        files_to_download.append((fname, f"{BASE_URL_WTA}/{fname}"))

    # Tour-level match files per year
    for year in range(year_start, year_end + 1):
        fname = f"wta_matches_{year}.csv"
        files_to_download.append((fname, f"{BASE_URL_WTA}/{fname}"))

    # Qualifying / ITF match files (available from 1968)
    for year in range(year_start, year_end + 1):
        fname = f"wta_matches_qual_itf_{year}.csv"
        files_to_download.append((fname, f"{BASE_URL_WTA}/{fname}"))

    _parallel_download(files_to_download, data_dir, force, progress_callback)


def load_players(tour="atp"):
    """Load player data from cached CSV."""
    data_dir = get_data_dir() / tour
    path = data_dir / f"{tour}_players.csv"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(
        path,
        dtype={"player_id": str, "dob": str, "wikidata_id": str},
        low_memory=False,
    )


def load_matches(tour="atp", year_start=1968, year_end=None,
                 include_qual=False):
    """Load match data from cached CSVs and concatenate into one DataFrame."""
    if year_end is None:
        year_end = datetime.date.today().year
    data_dir = get_data_dir() / tour
    dtype_map = {
        "winner_id": str, "loser_id": str,
        "winner_seed": str, "loser_seed": str,
    }
    frames = []

    def _read(path):
        if path.exists():
            frames.append(pd.read_csv(path, dtype=dtype_map, low_memory=False))

    for year in range(year_start, year_end + 1):
        # Tour-level singles
        _read(data_dir / f"{tour}_matches_{year}.csv")

        if include_qual:
            # ATP: qual_chall;  WTA: qual_itf
            _read(data_dir / f"{tour}_matches_qual_chall_{year}.csv")
            _read(data_dir / f"{tour}_matches_qual_itf_{year}.csv")
            # ATP futures
            _read(data_dir / f"{tour}_matches_futures_{year}.csv")

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_doubles(tour="atp", year_start=2000, year_end=2020):
    """Load doubles match data from cached CSVs."""
    data_dir = get_data_dir() / tour
    dtype_map = {
        "winner1_id": str, "winner2_id": str,
        "loser1_id": str, "loser2_id": str,
        "winner_seed": str, "loser_seed": str,
    }
    frames = []
    for year in range(year_start, year_end + 1):
        path = data_dir / f"{tour}_matches_doubles_{year}.csv"
        if path.exists():
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                frames.append(pd.read_csv(path, dtype=dtype_map, low_memory=False, index_col=False))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_rankings(tour="atp"):
    """Load rankings from cached CSVs (current + historical decades)."""
    data_dir = get_data_dir() / tour
    frames = []
    dtype_map = {"player": str}

    # Historical decade files
    decades = ("70s", "80s", "90s", "00s", "10s", "20s") if tour == "atp" \
        else ("80s", "90s", "00s", "10s", "20s")
    for decade in decades:
        path = data_dir / f"{tour}_rankings_{decade}.csv"
        if path.exists():
            frames.append(pd.read_csv(path, dtype=dtype_map, low_memory=False))

    # Current rankings
    path = data_dir / f"{tour}_rankings_current.csv"
    if path.exists():
        frames.append(pd.read_csv(path, dtype=dtype_map, low_memory=False))

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Live scraping from tennisabstract.com
# ---------------------------------------------------------------------------

_scraper_instance = None


def _get_scraper():
    global _scraper_instance
    if _scraper_instance is None:
        _scraper_instance = TennisAbstractScraper()
    return _scraper_instance


def _build_match_signature(df):
    """Digest recent completed matches; ignores scheduled/upcoming rows."""
    if df is None or df.empty:
        return None
    completed = df
    if "is_upcoming" in completed.columns:
        completed = completed[completed["is_upcoming"].fillna(0).astype(int) != 1]
    if completed.empty:
        return None
    fields = [
        "tourney_date", "tourney_name", "round", "winner_name",
        "loser_name", "score", "winner_id", "loser_id",
    ]
    sort_fields = [c for c in fields if c in completed.columns]
    completed = completed.sort_values(sort_fields, kind="stable")
    rows = []
    for _, row in completed.iterrows():
        rows.append("\x1f".join(
            str(row.get(col, "") or "").strip() for col in fields
        ))
    payload = "\x1e".join(rows)
    return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()


def _ranking_lookup(rankings):
    """Build normalized name -> current ranking metadata."""
    lookup = {}
    for entry in rankings or []:
        name = entry.get("name")
        if not name:
            continue
        lookup[_normalize_name(name)] = {
            "rank": entry.get("rank"),
            "points": entry.get("points"),
        }
    return lookup


def _fill_current_ranks(df, ranking_lookup):
    """Fill missing match ranks from the current top-N ranking snapshot."""
    if df is None or df.empty or not ranking_lookup:
        return df
    df = df.copy()
    for side in ("winner", "loser"):
        name_col = f"{side}_name"
        rank_col = f"{side}_rank"
        points_col = f"{side}_rank_points"
        if name_col not in df.columns:
            continue
        if rank_col not in df.columns:
            df[rank_col] = None
        if points_col not in df.columns:
            df[points_col] = None
        for idx, name in df[name_col].items():
            meta = ranking_lookup.get(_normalize_name(str(name or "")))
            if not meta:
                continue
            rank = meta.get("rank")
            points = meta.get("points")
            if rank is not None and pd.isna(df.at[idx, rank_col]):
                df.at[idx, rank_col] = rank
            if points is not None and pd.isna(df.at[idx, points_col]):
                df.at[idx, points_col] = points
    return df


def scrape_player_matches(player_name, min_year=None, tour="atp",
                          max_matches=None, match_provider=None):
    """
    Scrape all matches for a player from tennisabstract.com and return
    a DataFrame in the same format as the database 'matches' table.

    If *min_year* is set, only matches from that year onward are kept.
    If *max_matches* is set, only the N most recent matches are kept
    (after the min_year filter).

    Returns (DataFrame, last_match_date_str, match_signature).
    ``last_match_date_str`` is the YYYYMMDD string of the most recent
    match across ALL years (before the min_year filter), or None.
    ``match_signature`` is a digest of the recent completed matches and
    changes when a result appears even if TennisAbstract keeps the same
    tournament date for every match in the event.
    """
    provider = get_match_provider(match_provider)
    result = provider.fetch_player_matches(
        player_name, min_year=min_year, tour=tour,
        max_matches=max_matches,
    )
    logger.debug(
        "Match provider %s returned %d rows for %s",
        result.provider, 0 if result.df is None else len(result.df),
        player_name,
    )
    return result.df, result.last_match_date, _build_match_signature(result.df)


def scrape_current_rankings(tour="atp", discipline="singles", source="LIVE"):
    """
    Scrape rankings with points from live-tennis.eu.

    Falls back to tennisabstract.com (without points) only for singles LIVE.
    Returns a list of dicts: {rank, name, country, points, ...}
    """
    scraper = _get_scraper()
    # Primary source: live-tennis.eu (has points, age, diffs, next tournament)
    rankings = scraper.scrape_rankings_live_tennis(
        tour=tour, discipline=discipline, source=source
    )
    if rankings:
        return rankings
    # Fallback: tennisabstract (no points) for singles LIVE only
    if discipline == "singles" and source.upper() == "LIVE":
        html = scraper.fetch_rankings_html(tour)
        if not html:
            return []
        return scraper.parse_rankings(html, tour)
    return []


def _normalize_name(name):
    """Normalize a player name for matching across sources."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.replace("-", " ")
    return re.sub(r"\s+", " ", name).strip().lower()


def _strip_draw_size(token):
    """Remove the trailing draw-size annotation from a tournament label.

    The OFFICIAL ranking exposes the current/previous tournament as
    ``<Tournament> <Round>(<DrawSize>)``.  The ``(DrawSize)`` part flips
    while the event progresses (e.g. ``R32(R128)`` -> ``R32(R64)``)
    even when the player has not played a new match, so it must be
    excluded from the fingerprint comparison.
    """
    if not token:
        return ""
    return re.sub(r"\s*\([^)]*\)\s*$", "", token).strip()


def _make_activity_fingerprint(current_tournament, previous_tournament):
    """Build the stored activity fingerprint from OFFICIAL ranking text."""
    current = _strip_draw_size(current_tournament or "")
    previous = _strip_draw_size(previous_tournament or "")
    return f"{current}|{previous}"


_ROUND_ORDER = {
    "R128": 1,
    "R64": 2,
    "R32": 3,
    "R16": 4,
    "QF": 5,
    "SF": 6,
    "F": 7,
}

_CONFIRMED_ACTIVITY_REASONS = {
    "due_upcoming",
    "previous_result_with_new_current",
    "round_advanced",
    "loss_result",
    "title_result",
}


def _split_activity_fingerprint(fp):
    if not fp:
        return "", ""
    current, previous = (str(fp).split("|", 1) + [""])[:2]
    return _strip_draw_size(current), _strip_draw_size(previous)


def _parse_official_activity_token(token):
    """Parse live-tennis OFFICIAL activity text into semantic pieces."""
    clean = _strip_draw_size(token or "")
    parsed = {
        "raw": clean,
        "tournament": "",
        "round": "",
        "is_empty": not bool(clean),
        "is_loss": False,
        "is_title": False,
    }
    if not clean:
        return parsed

    body = clean
    if re.match(r"(?i)^lost\s+in\s+", body):
        parsed["is_loss"] = True
        body = re.sub(r"(?i)^lost\s+in\s+", "", body).strip()

    parts = body.rsplit(" ", 1)
    if len(parts) == 2:
        tournament, tail = parts[0].strip(), parts[1].strip().upper()
        if tail == "W":
            parsed["tournament"] = tournament
            parsed["round"] = "W"
            parsed["is_title"] = True
            return parsed
        if tail in _ROUND_ORDER or re.fullmatch(r"R\d+", tail or ""):
            parsed["tournament"] = tournament
            parsed["round"] = tail
            return parsed

    parsed["tournament"] = body
    return parsed


def _same_tournament(left, right):
    return bool(left and right and _normalize_name(left) == _normalize_name(right))


def _round_advanced(old_round, new_round):
    old_pos = _ROUND_ORDER.get((old_round or "").upper())
    new_pos = _ROUND_ORDER.get((new_round or "").upper())
    if old_pos is None or new_pos is None:
        return bool(old_round and new_round and old_round != new_round)
    return new_pos > old_pos


def _official_activity_status(old_fp, new_fp):
    """Decide whether an OFFICIAL fingerprint transition means played tennis.

    A new tournament in ``current`` alone is a scheduled first match, so it
    should be saved as a baseline but should not trigger TennisAbstract.
    Results in ``previous`` and same-event round advances do trigger scraping.
    """
    old_cur, old_prev = _split_activity_fingerprint(old_fp)
    new_cur, new_prev = _split_activity_fingerprint(new_fp)
    old_norm = f"{old_cur}|{old_prev}"
    new_norm = f"{new_cur}|{new_prev}"
    if old_norm == new_norm:
        return False, "unchanged"

    old_cur_info = _parse_official_activity_token(old_cur)
    old_prev_info = _parse_official_activity_token(old_prev)
    new_cur_info = _parse_official_activity_token(new_cur)
    new_prev_info = _parse_official_activity_token(new_prev)
    old_labels = {label for label in (old_cur, old_prev) if label}

    previous_result_is_new = (
        bool(new_prev)
        and (new_prev_info["is_loss"] or new_prev_info["is_title"])
        and new_prev not in old_labels
    )
    if previous_result_is_new:
        if new_cur and not _same_tournament(
                new_cur_info["tournament"], new_prev_info["tournament"]):
            return True, "previous_result_with_new_current"
        if new_prev_info["is_title"]:
            return True, "title_result"
        return True, "loss_result"

    if (_same_tournament(old_cur_info["tournament"],
                         new_cur_info["tournament"])
            and _round_advanced(old_cur_info["round"],
                                new_cur_info["round"])):
        return True, "round_advanced"

    if new_cur and not _same_tournament(
            old_cur_info["tournament"], new_cur_info["tournament"]):
        return False, "scheduled_new_tournament"

    return False, "unchanged"


def _activity_changed(old_fp, new_fp):
    """Pure-in-memory equivalent of ``has_(extended_)new_activity``.

    Returns True if *new_fp* contains tournament text not already
    present in *old_fp*.  Used by the bulk activity-check path so we
    don't issue 1000+ Turso round-trips per loop.

    The ``(DrawSize)`` suffix is stripped from each side before the
    comparison, so a draw-size update alone does not trigger a re-scrape.
    """
    if old_fp is None:
        return True
    if old_fp == new_fp:
        return False
    def _norm(fp):
        cur, prev = (fp.split("|", 1) + [""])[:2]
        return _strip_draw_size(cur), _strip_draw_size(prev)
    old_cur, old_prev = _norm(old_fp)
    new_cur, new_prev = _norm(new_fp)
    old_combined = f"{old_cur}{old_prev}"
    cur_is_new = bool(new_cur) and new_cur not in old_combined
    prev_is_new = bool(new_prev) and new_prev not in old_combined
    return cur_is_new or prev_is_new


def _cache_row_is_fresh(cache_row, expire_hours):
    """In-memory equivalent of ``is_(player|extended)_cache_valid``."""
    if not cache_row or expire_hours == 0:
        return False
    last_iso = cache_row[0]
    if not last_iso:
        return False
    try:
        last = datetime.datetime.fromisoformat(last_iso)
    except (ValueError, TypeError):
        return False
    return (datetime.datetime.now() - last
            < datetime.timedelta(hours=expire_hours))


def _cache_retry_count(cache_row):
    if not cache_row or len(cache_row) < 5:
        return 0
    try:
        return int(cache_row[4] or 0)
    except (TypeError, ValueError):
        return 0


def _cache_next_scrape_after(cache_row):
    if not cache_row or len(cache_row) < 6 or not cache_row[5]:
        return None
    try:
        return datetime.datetime.fromisoformat(cache_row[5])
    except (TypeError, ValueError):
        return None


def _retry_cooldown_active(cache_row, rank):
    """Return True when a rank>200 preserved retry must wait longer."""
    try:
        rank_num = int(rank or 9999)
    except (TypeError, ValueError):
        rank_num = 9999
    if rank_num <= 200:
        return False
    next_after = _cache_next_scrape_after(cache_row)
    return bool(next_after and datetime.datetime.now() < next_after)


def _next_retry_state(cache_row, rank):
    """Return (retry_count, next_after_iso) for an unconfirmed scrape."""
    try:
        rank_num = int(rank or 9999)
    except (TypeError, ValueError):
        rank_num = 9999
    if rank_num <= 200:
        return 0, None
    retry_count = _cache_retry_count(cache_row) + 1
    delay_hours = 24 if retry_count == 1 else 168
    next_after = datetime.datetime.now() + datetime.timedelta(hours=delay_hours)
    return retry_count, next_after.isoformat()


def _matches_changed(cache_row, match_signature):
    """Return True if the completed-match digest changed."""
    if not match_signature:
        return False
    if not cache_row or len(cache_row) < 4 or not cache_row[3]:
        return True
    return str(match_signature) != str(cache_row[3])


def _db_match_signature(db, player_name, tour="atp", limit=20):
    """Build the same completed-match digest from existing SCRAPED rows."""
    if db is None:
        return None
    try:
        rows = db.conn.execute(
            """
            SELECT tourney_date, tourney_name, round, winner_name,
                   loser_name, score, winner_id, loser_id, is_upcoming
            FROM matches
            WHERE tour = ?
              AND tourney_id = 'SCRAPED'
              AND (is_upcoming = 0 OR is_upcoming IS NULL)
              AND (winner_name = ? OR loser_name = ?)
            ORDER BY tourney_date DESC, match_num DESC
            LIMIT ?
            """,
            (tour, player_name, player_name, limit),
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    return _build_match_signature(pd.DataFrame(rows, columns=[
        "tourney_date", "tourney_name", "round", "winner_name",
        "loser_name", "score", "winner_id", "loser_id", "is_upcoming",
    ]))


def _resolve_ranking_name(ranking_name, db, tour="atp"):
    """Resolve a live-tennis.eu ranking name to the full DB player name.

    Rankings often carry truncated names (e.g. "Daniel Mérida" instead of
    "Daniel Merida Aguilar").  We look up the ``players`` table for a row
    whose normalized ``name_first + ' ' + name_last`` starts with (or
    equals) the normalized ranking name.

    Returns the canonical ``name_first + ' ' + name_last`` if found,
    otherwise the original *ranking_name* unchanged.
    """
    if db is None:
        return ranking_name
    norm = _normalize_name(ranking_name)
    try:
        rows = db.conn.execute(
            "SELECT name_first, name_last FROM players WHERE tour = ?",
            (tour,),
        ).fetchall()
        for r in rows:
            first = r[0] or ""
            last = r[1] or ""
            full = f"{first} {last}".strip()
            nfull = _normalize_name(full)
            if nfull == norm:
                return full          # exact match
            if nfull.startswith(norm + " "):
                return full          # ranking name is a prefix
        # Second pass: ranking first matches, ranking last is prefix of DB last
        parts = norm.split()
        if len(parts) >= 2:
            r_first = parts[0]
            r_last = " ".join(parts[1:])
            for r in rows:
                first = r[0] or ""
                last = r[1] or ""
                nf = _normalize_name(first)
                nl = _normalize_name(last)
                if nf == r_first and nl.startswith(r_last):
                    return f"{first} {last}".strip()
    except Exception:
        pass
    return ranking_name


def _build_ranking_name_map(ranking_names, db, tour="atp"):
    """Batch-resolve a list of ranking display names to full DB names.

    Returns a dict mapping each original ranking name to its resolved
    full name (unchanged if no DB match is found).
    """
    if db is None:
        return {n: n for n in ranking_names}

    # Build normalized lookup from players table (once)
    try:
        rows = db.conn.execute(
            "SELECT name_first, name_last FROM players WHERE tour = ?",
            (tour,),
        ).fetchall()
    except Exception:
        return {n: n for n in ranking_names}

    # Two indexes: exact normalized name → full, and prefix-friendly list
    exact_map = {}   # norm_full → canonical full name
    prefix_list = [] # (norm_full, norm_first, norm_last, canonical)
    for r in rows:
        first = r[0] or ""
        last = r[1] or ""
        full = f"{first} {last}".strip()
        if not full:
            continue
        nfull = _normalize_name(full)
        exact_map[nfull] = full
        prefix_list.append((
            nfull,
            _normalize_name(first),
            _normalize_name(last),
            full,
        ))

    result = {}
    for rname in ranking_names:
        norm = _normalize_name(rname)
        # 1. Exact normalized match — keep the original ranking name
        #    (_clean_name already handles accent differences)
        if norm in exact_map:
            result[rname] = rname
            continue
        # 2. Full normalized DB name starts with ranking name
        #    → ranking name is truncated, use the longer DB name
        found = None
        for nfull, _, _, canonical in prefix_list:
            if nfull.startswith(norm + " "):
                found = canonical
                break
        if found:
            result[rname] = found
            continue
        # 3. First name matches, last-name is a prefix of DB last
        parts = norm.split()
        if len(parts) >= 2:
            r_first = parts[0]
            r_last = " ".join(parts[1:])
            for _, nf, nl, canonical in prefix_list:
                if nf == r_first and nl.startswith(r_last):
                    found = canonical
                    break
        result[rname] = found if found else rname
    return result


def scrape_top_players_matches(top_n=50, tour="atp", progress_callback=None,
                               db=None, cache_expire_hours=6, min_year=None,
                               max_matches_per_player=20,
                               max_workers=8, return_report=False,
                               match_provider=None):
    """
    Scrape matches for the top N ranked players.

    Uses OFFICIAL rankings to detect activity (event-driven scraping):
    - Changed activity fingerprint → scrape (new results available)
    - Same fingerprint → skip (no new data)
    - Both current/previous empty → inactive, 7-day cache fallback

    If *db* is provided, uses the scrape cache to skip players whose
    data was scraped less than *cache_expire_hours* ago.
    If *min_year* is set, only matches from that year onward are kept.
    If *max_matches_per_player* is set, only the N most recent matches
    per player are kept (default 20). Reduces import work since older
    scraped matches are already in the DB. Set to None for full history.

    Returns (combined_DataFrame, rankings_list, scraped_names_list).  If
    *return_report* is true, appends a fourth value with scrape counters.
    """
    selected_match_provider = (match_provider or os.getenv(MATCH_PROVIDER_ENV)
                               or "tennisabstract").strip().lower()
    selected_match_provider = {
        "ta": "tennisabstract",
        "tennis_abstract": "tennisabstract",
        "sf": "sofascore",
        "sofa": "sofascore",
    }.get(selected_match_provider, selected_match_provider)

    report = {
        "tour": tour,
        "top_n": top_n,
        "match_provider": selected_match_provider,
        "rankings": 0,
        "source": None,
        "stale": 0,
        "stale_reasons": {},
        "activity_statuses": {},
        "retry_cooldown": 0,
        "skipped": 0,
        "attempted": 0,
        "confirmed": 0,
        "ta_lag": 0,
        "not_found": 0,
        "empty": 0,
        "errors": 0,
        "access_blocked": 0,
        "rows": 0,
        "confirmed_players": [],
    }

    def _finish(result):
        if return_report:
            return (*result, report)
        return result

    # Use OFFICIAL rankings as primary source (stable ordering by official rank)
    # Fall back to LIVE only if OFFICIAL fails.
    used_official = False
    rankings = scrape_current_rankings(tour, discipline="singles",
                                       source="OFFICIAL")
    if rankings:
        used_official = True
    else:
        logger.info("OFFICIAL rankings unavailable, falling back to LIVE")
        rankings = scrape_current_rankings(tour)
    if not rankings:
        logger.warning("Could not fetch rankings for scraping")
        return _finish((pd.DataFrame(), [], []))

    # Sort by rank to ensure correct top-N selection, then hard-filter
    # by rank value to handle cases where the rank field doesn't match
    # list position (e.g. LIVE ranking projections).
    rankings.sort(key=lambda e: e.get("rank", 9999))
    rankings = [e for e in rankings if e.get("rank", 9999) <= top_n]
    report["rankings"] = len(rankings)
    report["source"] = "OFFICIAL" if used_official else "LIVE"

    # Resolve ranking display names to full DB names
    name_map = _build_ranking_name_map(
        [e["name"] for e in rankings], db, tour=tour)
    for entry in rankings:
        entry["name"] = name_map.get(entry["name"], entry["name"])

    player_names = [e["name"] for e in rankings]
    rank_by_name = {e["name"]: e.get("rank") for e in rankings}
    current_rank_lookup = _ranking_lookup(rankings)
    logger.info("Selected %d players for scraping (ranks %s\u2013%s: %s \u2026 %s, source=%s)",
                len(rankings),
                rankings[0].get("rank") if rankings else "?",
                rankings[-1].get("rank") if rankings else "?",
                rankings[0]["name"] if rankings else "?",
                rankings[-1]["name"] if rankings else "?",
                "OFFICIAL" if used_official else "LIVE")

    # ---- Event-driven activity detection via OFFICIAL rankings ----
    # Only build fingerprints from OFFICIAL data (has current/previous tournament).
    # If we fell back to LIVE, fetch OFFICIAL separately for fingerprinting.
    activity_map = {}  # normalized_name → fingerprint string
    if db is not None:
        official_entries = rankings if used_official else []
        if not used_official:
            try:
                scraper = _get_scraper()
                official_entries = scraper.scrape_rankings_live_tennis(
                    tour=tour, discipline="singles", source="OFFICIAL"
                )
                logger.info("Fetched OFFICIAL rankings (%d) for activity detection",
                            len(official_entries))
            except Exception as exc:
                logger.warning("Could not fetch OFFICIAL rankings for activity "
                               "detection: %s", exc)
        for entry in official_entries:
            cur_t = entry.get("current_tournament", "")
            prev_t = entry.get("previous_tournament", "")
            fp = _make_activity_fingerprint(cur_t, prev_t)
            norm = _normalize_name(entry["name"])
            activity_map[norm] = fp
        logger.info("Built activity map for %d players", len(activity_map))

    # Determine which players need re-scraping
    stale = set()
    stale_reasons = {}
    fingerprints = {}  # name → fingerprint (for players we'll scrape)

    # Bulk-load the entire scrape_cache in ONE round-trip instead of
    # issuing two queries per player (1000+ remote calls for top-1000).
    cache_snapshot = db.get_all_scrape_cache() if db is not None else {}

    def _scrape_cache_lookup(name):
        storage_name = clean_player_name(name) or name
        if storage_name in cache_snapshot:
            return storage_name, cache_snapshot[storage_name]
        if name in cache_snapshot:
            return name, cache_snapshot[name]
        return storage_name, None

    cache_keys = {}
    cache_rows = {}

    # Players with at least one upcoming match whose date is <= today.
    # Those rows should now have a completed result on tennisabstract,
    # so force a re-scrape regardless of fingerprint state.  This catches
    # the case where a player advances within the same tournament and
    # the OFFICIAL fingerprint stays identical for hours/days.
    # NOTE: DB rows store the diacritic-stripped CSV-canonical spelling,
    # while ranking names may carry accents (e.g. "Rafael J\u00f3dar" vs
    # "Rafael Jodar").  Normalise both sides for the membership check.
    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    _due_raw = (db.get_players_with_due_upcoming(today_str)
                if db is not None else set())
    due_upcoming = {_normalize_name(n) for n in _due_raw}

    for name in player_names:
        norm = _normalize_name(name)
        fp = activity_map.get(norm)

        if db is None:
            stale.add(name)
            stale_reasons[name] = "no_db"
            continue

        cache_key, cache_row = _scrape_cache_lookup(name)
        cache_keys[name] = cache_key
        cache_rows[name] = cache_row
        # Trigger re-scrape whenever a local upcoming placeholder is due.
        # Confirmation is handled after the HTTP scrape by comparing the
        # completed-match signature; unconfirmed scrapes are not imported,
        # so the upcoming placeholder stays available for the next retry.
        has_due_upcoming = norm in due_upcoming

        if fp is not None:
            # We have activity data from OFFICIAL
            cur_t, prev_t = fp.split("|", 1)
            if not cur_t and not prev_t:
                # Inactive player: use 7-day cache
                if has_due_upcoming or not _cache_row_is_fresh(cache_row, 168):
                    stale.add(name)
                    stale_reasons[name] = (
                        "due_upcoming" if has_due_upcoming else "time")
                    fingerprints[name] = fp
            else:
                activity_changed, status = _official_activity_status(
                    cache_row[1] if cache_row else None, fp)
                report["activity_statuses"][status] = (
                    report["activity_statuses"].get(status, 0) + 1)
                if not activity_changed:
                    fingerprints[name] = fp
                    logger.debug("Skipping %s (%s)", name, status)
                    continue
                if _retry_cooldown_active(cache_row, rank_by_name.get(name)):
                    report["retry_cooldown"] += 1
                    logger.info(
                        "Skipping %s (retry cooldown until %s)",
                        name, cache_row[5])
                    continue

                # OFFICIAL indicates a played result.  Confirmation is still
                # handled after the HTTP scrape by comparing completed-match
                # signatures.  A due upcoming placeholder alone is not enough
                # here: OFFICIAL current-only text is usually just a scheduled
                # first round, not a played match.
                stale.add(name)
                stale_reasons[name] = status
                fingerprints[name] = fp
        else:
            # Player not found in OFFICIAL: fall back to time-based cache
            if not _cache_row_is_fresh(cache_row, cache_expire_hours):
                stale.add(name)
                stale_reasons[name] = "time"

    logger.info("Event-driven check: %d/%d players (top %d) need refresh",
                len(stale), len(player_names), top_n)
    report["stale"] = len(stale)
    for reason in stale_reasons.values():
        report["stale_reasons"][reason] = (
            report["stale_reasons"].get(reason, 0) + 1)

    all_frames = []
    scraped_names = []  # only the players we actually re-scraped
    scrape_idx = 0  # counter for actually scraped players

    # ---- Sync fingerprints for skipped (still-fresh) players ----
    skipped_updates = []
    actual_targets = []  # (idx, entry) tuples that need scraping
    for idx, entry in enumerate(rankings):
        name = entry["name"]
        if name not in stale:
            fp = fingerprints.get(name)
            if db is not None and fp is not None:
                cache_key = cache_keys.get(name) or (clean_player_name(name) or name)
                skipped_updates.append((fp, cache_key))
            logger.info("Skipping %s (cache still valid)", name)
            report["skipped"] += 1
            continue
        actual_targets.append((idx, entry))

    if db is not None and skipped_updates:
        now_iso = datetime.datetime.now().isoformat()
        with db._write_lock:
            db.conn.executemany(
                "INSERT INTO scrape_cache "
                "(player_name, last_scraped, match_count, "
                "activity_fingerprint) "
                "VALUES (?, ?, 0, ?) "
                "ON CONFLICT(player_name) DO UPDATE SET "
                "activity_fingerprint = excluded.activity_fingerprint, "
                "scrape_retry_count = 0, "
                "next_scrape_after = NULL",
                [(n, now_iso, fp) for fp, n in skipped_updates])
            db.conn.commit()

    # ---- Parallel HTTP fetches for stale players ----
    # cloudscraper sessions are thread-safe for concurrent GETs; we keep
    # the worker count modest to avoid hammering tennisabstract.com.
    total_to_scrape = len(actual_targets)
    worker_count = min(max(1, max_workers), max(1, total_to_scrape))

    def _fetch_one(entry):
        name = entry["name"]
        try:
            df, last_match_date, match_signature = scrape_player_matches(
                name, min_year=min_year, tour=tour,
                max_matches=max_matches_per_player,
                match_provider=match_provider)
            if selected_match_provider in {"sofascore", "hybrid"}:
                df = _fill_current_ranks(df, current_rank_lookup)
                match_signature = _build_match_signature(df)
            return name, df, last_match_date, match_signature, None
        except Exception as exc:
            return name, None, None, None, exc

    if total_to_scrape > 0:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {pool.submit(_fetch_one, entry): entry
                       for _, entry in actual_targets}
            for future in as_completed(futures):
                scrape_idx += 1
                name, df, last_match_date, match_signature, exc = future.result()

                if progress_callback:
                    progress_callback(scrape_idx, total_to_scrape,
                                      f"Scraped {name} "
                                      f"({scrape_idx}/{total_to_scrape})")

                if exc is not None:
                    logger.warning("Failed to scrape %s: %s", name, exc)
                    report["errors"] += 1
                    if isinstance(exc, TennisAbstractAccessError):
                        report["access_blocked"] += 1
                    continue
                report["attempted"] += 1

                # Decide whether to persist the new fingerprint.
                #
                # If the scrape produced real matches and changed the recent
                # completed-match digest: store the new fingerprint so future runs can
                # detect further activity changes.
                #
                # If the scrape did not change completed matches but the player
                # already had a cache entry (i.e. fingerprint changed but
                # tennisabstract hasn't published the result yet, while older
                # matches are still returned):
                # preserve the OLD fingerprint by passing None.  This
                # ensures the next run will see the activity change again
                # and retry, instead of treating the empty scrape as
                # "already covered".
                #
                # If there was no prior cache (first-ever scrape) and we
                # got nothing: store the current fingerprint as a
                # negative-cache marker to avoid retry storms on players
                # who simply have no tennisabstract presence.
                non_empty = df is not None and not df.empty
                cache_key = cache_keys.get(name) or (clean_player_name(name) or name)
                cache_row = cache_rows.get(name)
                had_cache = cache_row is not None
                needs_confirmed_activity = (
                    stale_reasons.get(name) in _CONFIRMED_ACTIVITY_REASONS)
                baseline_signature = None
                if (needs_confirmed_activity and had_cache
                        and (not cache_row or len(cache_row) < 4
                             or not cache_row[3])):
                    baseline_signature = _db_match_signature(
                        db, cache_key, tour,
                        limit=max_matches_per_player or 20)
                compare_row = cache_row
                if baseline_signature:
                    compare_row = (
                        cache_row[0], cache_row[1], cache_row[2],
                        baseline_signature,
                    )
                changed_matches = _matches_changed(compare_row, match_signature)
                confirmed = (
                    non_empty
                    and (changed_matches or not needs_confirmed_activity)
                )
                # Player not found on tennisabstract at all: both
                # last_match_date and match_signature are None (set by the
                # early-return path in scrape_player_matches when raw is None).
                # Store the fingerprint unconditionally so we don't retry the
                # same player on every run (negative-cache marker).
                ta_not_found = (
                    not non_empty and exc is None
                    and match_signature is None and last_match_date is None
                )
                fp_to_store = (fingerprints.get(name)
                               if confirmed or not had_cache or ta_not_found
                               else None)
                retry_count = None
                next_scrape_after = None
                if (fp_to_store is None and needs_confirmed_activity
                        and had_cache and not ta_not_found):
                    retry_count, next_scrape_after = _next_retry_state(
                        cache_row, futures[future].get("rank"))
                if db is not None:
                    try:
                        db.update_scrape_cache(
                            cache_key,
                            len(df) if df is not None else 0,
                            last_match_date=last_match_date,
                            match_signature=(match_signature
                                             if fp_to_store is not None else None),
                            activity_fingerprint=fp_to_store,
                            scrape_retry_count=retry_count,
                            next_scrape_after=next_scrape_after,
                        )
                    except Exception as cache_exc:
                        logger.warning(
                            "Could not update scrape cache for %s: %s "
                            "(will retry next run)", name, cache_exc)
                if confirmed:
                    all_frames.append(df)
                    scraped_names.append(name)
                    report["confirmed"] += 1
                    report["rows"] += len(df)
                    report["confirmed_players"].append(name)
                    logger.info("Scraped %d matches for %s", len(df), name)
                elif non_empty:
                    report["ta_lag"] += 1
                    logger.info(
                        "Scraped %d matches for %s, but completed matches "
                        "did not change (fingerprint preserved for retry)",
                        len(df), name)
                elif ta_not_found:
                    report["not_found"] += 1
                    logger.info(
                        "Player %s not found on tennisabstract "
                        "(fingerprint cached to suppress future retries)", name)
                else:
                    report["empty"] += 1
                    if had_cache:
                        logger.info(
                            "No new matches for %s "
                            "(fingerprint preserved for retry)", name)
                    else:
                        logger.info("No recent matches for %s", name)

    if progress_callback:
        progress_callback(len(stale), len(stale), "Scraping complete!")

    # Commit any fingerprint updates for skipped players
    if db is not None:
        try:
            db.conn.commit()
        except Exception as commit_exc:
            logger.warning("Could not commit fingerprint updates: %s", commit_exc)

    combined = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()
    return _finish((combined, rankings, scraped_names))


# ---------------------------------------------------------------------------
# Extended stats scraping (player-more.cgi)
# ---------------------------------------------------------------------------

def scrape_top_players_extended_stats(top_n=150, tour="atp",
                                      progress_callback=None, db=None,
                                      stop_event=None, rankings=None,
                                      priority_players=None, budget=None,
                                      inactive_budget=None,
                                      return_report=False):
    """
    Scrape extended stats for the top N ranked players, using the same
    activity-fingerprint logic as match scraping to skip unchanged players.

    Parameters
    ----------
    top_n : int
        Number of top-ranked players to process.
    tour : str
        "atp" or "wta"
    progress_callback : callable, optional
        Called with (current, total, message)
    db : TennisDatabase, optional
        Database instance for caching and storage.
    stop_event : threading.Event, optional
        If set, the loop will abort early (for graceful shutdown).

    Returns list of player names that were actually scraped.  If
    *return_report* is true, appends a second value with scrape counters.
    """
    report = {
        "tour": tour,
        "top_n": top_n,
        "rankings": 0,
        "candidates": 0,
        "selected": 0,
        "skipped_budget": 0,
        "reasons": {},
        "activity_statuses": {},
        "scraped": 0,
        "empty": 0,
        "errors": 0,
        "rows": 0,
    }

    def _finish(result):
        if return_report:
            return result, report
        return result

    if rankings is None:
        rankings = scrape_current_rankings(tour, discipline="singles",
                                           source="OFFICIAL")
        if not rankings:
            rankings = scrape_current_rankings(tour)
    if not rankings:
        return _finish([])

    rankings.sort(key=lambda e: e.get("rank", 9999))
    rankings = [e for e in rankings if e.get("rank", 9999) <= top_n]
    report["rankings"] = len(rankings)

    # Resolve ranking display names to full DB names
    name_map = _build_ranking_name_map(
        [e["name"] for e in rankings], db, tour=tour)
    for entry in rankings:
        entry["name"] = name_map.get(entry["name"], entry["name"])

    # Build activity fingerprint map
    activity_map = {}
    for entry in rankings:
        cur_t = entry.get("current_tournament", "")
        prev_t = entry.get("previous_tournament", "")
        fp = _make_activity_fingerprint(cur_t, prev_t)
        norm = _normalize_name(entry["name"])
        activity_map[norm] = fp

    # Determine which players need scraping
    stale = []
    fingerprints = {}
    skipped_updates = []
    priority_norms = {_normalize_name(n) for n in (priority_players or [])}

    # Bulk-load extended_stats_cache in ONE round-trip (avoids 1000+
    # remote calls for top-1000 against Turso).
    cache_snapshot = (db.get_all_extended_stats_cache()
                      if db is not None else {})

    for entry in rankings:
        name = entry["name"]
        storage_name = clean_player_name(name) or name
        norm = _normalize_name(name)
        fp = activity_map.get(norm)
        rank = entry.get("rank", 9999)
        reason = None

        if db is None:
            reason = "no_db"
            fingerprints[name] = fp
        elif norm in priority_norms:
            reason = "match_confirmed"
            fingerprints[name] = fp
        else:
            cache_row = cache_snapshot.get(storage_name) or cache_snapshot.get(name)

            if fp is not None:
                cur_t, prev_t = fp.split("|", 1)
                if not cur_t and not prev_t:
                    # Inactive: use time-based cache (1 week)
                    if not _cache_row_is_fresh(cache_row, 168):
                        reason = "inactive_time"
                        fingerprints[name] = fp
                else:
                    activity_changed, status = _official_activity_status(
                        cache_row[1] if cache_row else None, fp)
                    report["activity_statuses"][status] = (
                        report["activity_statuses"].get(status, 0) + 1)
                    fingerprints[name] = fp
                    if activity_changed:
                        reason = status
                    else:
                        skipped_updates.append((storage_name, fp))
            else:
                if not _cache_row_is_fresh(cache_row, 168):
                    reason = "time"

        if reason:
            stale.append({"name": name, "rank": rank, "reason": reason})
            report["reasons"][reason] = report["reasons"].get(reason, 0) + 1

    report["candidates"] = len(stale)

    if db is not None and skipped_updates:
        now_iso = datetime.datetime.now().isoformat()
        with db._write_lock:
            db.conn.executemany(
                "INSERT INTO extended_stats_cache "
                "(player_name, last_scraped, tables_scraped, "
                "activity_fingerprint) "
                "VALUES (?, ?, '', ?) "
                "ON CONFLICT(player_name) DO UPDATE SET "
                "activity_fingerprint = excluded.activity_fingerprint",
                [(name, now_iso, fp) for name, fp in skipped_updates])
            db.conn.commit()

    if budget is not None or inactive_budget is not None:
        selected = []
        inactive_selected = 0
        total_limit = budget if budget is not None else len(stale)
        inactive_limit = inactive_budget if inactive_budget is not None else len(stale)
        reason_order = {
            "match_confirmed": 0,
            "activity": 1,
            "no_db": 2,
            "time": 3,
            "inactive_time": 4,
        }
        for item in sorted(stale, key=lambda x: (
                reason_order.get(x["reason"], 99), x["rank"])):
            if len(selected) >= total_limit:
                break
            if item["reason"] == "inactive_time":
                if inactive_selected >= inactive_limit:
                    continue
                inactive_selected += 1
            selected.append(item)
        report["skipped_budget"] = len(stale) - len(selected)
        stale = selected

    report["selected"] = len(stale)

    scraped_names = []
    for idx, item in enumerate(stale):
        name = item["name"]
        if stop_event and stop_event.is_set():
            logger.info("Extended stats background scrape stopped early")
            break

        if progress_callback:
            progress_callback(idx, len(stale),
                              f"Extended stats: {name} "
                              f"({idx + 1}/{len(stale)})...")
        try:
            result = scrape_player_extended_stats(
                name, db=db, tour=tour, force=True,
                activity_fingerprint=fingerprints.get(name),
            )
            scraped_names.append(name)
            rows = sum(result.values()) if result else 0
            report["rows"] += rows
            if rows:
                report["scraped"] += 1
            else:
                report["empty"] += 1
        except Exception as exc:
            logger.warning("Failed extended stats for %s: %s", name, exc)
            report["errors"] += 1

    logger.info("Extended stats scrape done: %d/%d players scraped",
                len(scraped_names), len(stale))
    return _finish(scraped_names)

def scrape_player_extended_stats(player_name, db=None, tables=None,
                                 tour="atp", progress_callback=None,
                                 force=False, activity_fingerprint=None):
    """
    Scrape extended statistics for a player from tennisabstract.com
    player-more.cgi endpoint.

    Parameters
    ----------
    player_name : str
        Player name (e.g. "Jannik Sinner")
    db : TennisDatabase, optional
        Database to store results. If None, returns raw data only.
    tables : list[str], optional
        Specific tables to scrape. None = all available.
    tour : str
        "atp" or "wta"
    progress_callback : callable, optional
        Called with (current, total, message)
    force : bool
        If True, scrape even if cache is fresh.
    activity_fingerprint : str, optional
        Current activity fingerprint for smart cache invalidation.

    Returns dict of table_name -> record_count
    """
    from .scraper import EXTENDED_CONVERTERS, EXTENDED_DB_TABLES, clean_player_name

    # Normalize the name for all DB writes/lookups so that scraping
    # "Rafael Jódar" stores rows under "Rafael Jodar" (matching the
    # diacritic-free spelling used in the players table).
    storage_name = clean_player_name(player_name) or player_name

    # Check cache using fingerprint if available, else time-based
    if db and not force:
        if activity_fingerprint:
            if not db.has_extended_new_activity(storage_name,
                                               activity_fingerprint):
                logger.info("Extended stats fingerprint unchanged for %s",
                            storage_name)
                return {}
        elif db.is_extended_cache_valid(storage_name):
            logger.info("Extended stats cache still valid for %s",
                        storage_name)
            return {}

    scraper = _get_scraper()
    raw_data = scraper.fetch_all_extended_tables(
        player_name, tables=tables, progress_callback=progress_callback,
        tour=tour)

    if not raw_data:
        logger.warning("No extended stats found for %s", player_name)
        # Always advance the cached fingerprint to the current one, even
        # on an empty result.  Otherwise players without a player-more.cgi
        # page (no extended stats available on tennisabstract) trigger a
        # re-scrape on every run as soon as their tournament label changes.
        # The next retry will only happen when the activity fingerprint
        # actually changes again (e.g. new tournament).
        if db:
            try:
                db.update_extended_stats_cache(
                    storage_name, [],
                    activity_fingerprint=activity_fingerprint)
            except Exception:
                logger.exception(
                    "Failed to cache empty extended stats for %s",
                    storage_name)
        return {}

    result = {}
    tables_scraped = []

    for table_name, rows in raw_data.items():
        converter = EXTENDED_CONVERTERS.get(table_name)
        db_table = EXTENDED_DB_TABLES.get(table_name)
        if not converter or not db_table:
            continue

        records = converter(rows, storage_name, tour=tour)
        if records and db:
            count = db.import_extended_stats(db_table, records, storage_name)
            result[table_name] = count
            tables_scraped.append(table_name)
        elif records:
            result[table_name] = len(records)
            tables_scraped.append(table_name)

    # Update cache.  Always advance the fingerprint to the current one,
    # even when no converters produced records for this fingerprint state.
    # The next retry will happen automatically when activity changes again.
    if db:
        db.update_extended_stats_cache(
            storage_name, tables_scraped,
            activity_fingerprint=activity_fingerprint)

    logger.info("Extended stats for %s: %s", storage_name, result)
    return result
