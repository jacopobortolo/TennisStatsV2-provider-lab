"""
Cloud → local sync.

Downloads the live data from Turso and merges it into the local SQLite
database (the one with the historical Sackmann CSVs).

Strategy per table:
    matches        : DELETE WHERE tourney_id='SCRAPED'  → bulk INSERT cloud rows
    rankings       : DELETE WHERE ranking_date='LIVE'   → bulk INSERT cloud rows
    players        : INSERT OR REPLACE (PK player_id+tour)
    scrape_cache   : DELETE * → INSERT cloud rows (PK player_name)
    extended_stats_cache : same
    match_winners_errors / match_serve_speed / match_pbp_stats /
    match_mcp_serve / match_mcp_return / match_mcp_rally /
    match_mcp_tactics  : DELETE * → INSERT cloud rows
        (these tables only ever contain scraped data, so a full replace
         is safe and avoids tricky de-duplication.)

The merge runs in a single SQLite transaction so a failure mid-way leaves
the local DB untouched.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# Tables whose contents are *entirely* scraped (no CSV-historical rows).
# We mirror them by full-replace.
FULL_REPLACE_TABLES = (
    "scrape_cache",
    "extended_stats_cache",
    "match_winners_errors",
    "match_serve_speed",
    "match_pbp_stats",
    "match_mcp_serve",
    "match_mcp_return",
    "match_mcp_rally",
    "match_mcp_tactics",
)

# Tables with primary key — INSERT OR REPLACE is enough.
UPSERT_TABLES = (
    "players",  # PK (player_id, tour)
)


def _table_exists(local: sqlite3.Connection, name: str) -> bool:
    cur = local.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,))
    return cur.fetchone() is not None


def _remote_columns(client, table: str) -> list[str]:
    rs = client.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in rs.rows]


def _local_columns(local: sqlite3.Connection, table: str) -> list[str]:
    cur = local.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def _stream_rows(client, sql: str, params: list, page: int = 5000):
    """Yield (columns, rows_chunk) pages from a remote SELECT."""
    offset = 0
    while True:
        paged_sql = f"{sql} LIMIT ? OFFSET ?"
        rs = client.execute(paged_sql, params + [page, offset])
        if not rs.rows:
            return
        yield rs.columns, [tuple(r) for r in rs.rows]
        if len(rs.rows) < page:
            return
        offset += page


def _copy_table(
    client,
    local: sqlite3.Connection,
    table: str,
    where: Optional[str] = None,
    upsert: bool = False,
    delete_local: bool = True,
    progress_callback: Optional[Callable] = None,
) -> int:
    """Copy a table from remote to local. Returns number of rows inserted."""
    if not _table_exists(local, table):
        logger.info("  %s: not present locally, skipping", table)
        return 0

    remote_cols = _remote_columns(client, table)
    if not remote_cols:
        logger.info("  %s: not present remotely, skipping", table)
        return 0
    local_cols = set(_local_columns(local, table))
    cols = [c for c in remote_cols if c in local_cols and c.lower() != "id"]
    if not cols:
        return 0

    sel_sql = f"SELECT {', '.join(cols)} FROM {table}"
    sel_params: list = []
    if where:
        sel_sql += f" WHERE {where}"

    if delete_local:
        if where:
            local.execute(f"DELETE FROM {table} WHERE {where}")
        else:
            local.execute(f"DELETE FROM {table}")

    verb = "INSERT OR REPLACE" if upsert else "INSERT"
    placeholders = ", ".join("?" for _ in cols)
    insert_sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"

    total = 0
    for _, page_rows in _stream_rows(client, sel_sql, sel_params):
        local.executemany(insert_sql, page_rows)
        total += len(page_rows)
        if progress_callback:
            progress_callback(table, total)

    logger.info("  %s: %d rows merged", table, total)
    return total


def _fix_numeric_wta_scraped_tour(local: sqlite3.Connection) -> int:
    from tennis_app.core.database import _player_match_key

    wta_names = set()
    atp_names = set()
    for first, last, tour in local.execute("""
        SELECT name_first, name_last, tour
        FROM players
        WHERE tour IN ('atp', 'wta')
    """):
        full_name = f"{first or ''} {last or ''}".strip()
        if not full_name:
            continue
        key = _player_match_key(full_name)
        if tour == "wta":
            wta_names.add(key)
        else:
            atp_names.add(key)

    if not wta_names:
        return 0

    candidates = local.execute("""
        SELECT DISTINCT winner_name, loser_name
        FROM matches
        WHERE tourney_id = 'SCRAPED'
          AND tour = 'atp'
          AND tourney_level GLOB '[0-9]*'
          AND winner_name IS NOT NULL
          AND loser_name IS NOT NULL
    """).fetchall()

    changed = 0
    for winner_name, loser_name in candidates:
        winner_key = _player_match_key(winner_name)
        loser_key = _player_match_key(loser_name)
        winner_is_wta = winner_key in wta_names and winner_key not in atp_names
        loser_is_wta = loser_key in wta_names and loser_key not in atp_names
        if winner_is_wta or loser_is_wta:
            local.execute("""
                UPDATE matches
                SET tour = 'wta'
                WHERE tourney_id = 'SCRAPED'
                  AND tour = 'atp'
                  AND tourney_level GLOB '[0-9]*'
                  AND winner_name = ?
                  AND loser_name = ?
            """, (winner_name, loser_name))
            changed += local.execute("SELECT changes()").fetchone()[0]
    return changed


def _canonicalize_scraped_matches(local: sqlite3.Connection) -> int:
    aliases = [
        ("monte carlo", "Monte Carlo", "Monte Carlo Masters", "Monte Carlo"),
        ("indian wells", "Indian Wells", "Indian Wells Masters", "Indian Wells"),
        ("miami", "Miami", "Miami Masters", "Miami"),
        ("madrid", "Madrid", "Madrid Masters", "WTA Madrid"),
        ("rome", "Rome", "Rome Masters", "WTA Rome"),
        ("italian open", "Rome", "Rome Masters", "WTA Rome"),
        ("cincinnati", "Cincinnati", "Cincinnati Masters", "Cincinnati"),
        ("shanghai", "Shanghai", "Shanghai Masters", "Shanghai"),
        ("paris masters", "Paris", "Paris Masters", "Paris"),
    ]
    changed = 0
    for pattern, city, atp_name, wta_name in aliases:
        labels = {atp_name.lower(), wta_name.lower(), city.lower()}
        if pattern == "italian open":
            labels.add("italian open")
        if pattern == "paris masters":
            labels = {"paris masters"}
        placeholders = ",".join("?" for _ in labels)
        local.execute(
            f"""
            UPDATE matches
            SET tourney_name = CASE
                WHEN tourney_level = 'C' THEN ?
                WHEN tourney_level GLOB '[0-9]*' THEN
                    CASE WHEN tour = 'wta'
                         THEN 'W' || tourney_level || ' ' || ?
                         ELSE 'M' || tourney_level || ' ' || ?
                    END
                WHEN tourney_level = 'M' THEN ?
                WHEN tourney_level IN ('PM', 'P', 'W') THEN ?
                ELSE ?
            END
            WHERE tourney_id = 'SCRAPED'
              AND LOWER(tourney_name) IN ({placeholders})
            """,
            (f"{city} CH", city, city, atp_name, wta_name, city, *labels),
        )
        changed += local.execute("SELECT changes()").fetchone()[0]

    local.execute("""
        UPDATE matches
        SET tour = 'wta'
        WHERE tourney_id = 'SCRAPED'
          AND tourney_name IN (
              'WTA Madrid', 'WTA Rome', 'Indian Wells', 'Miami',
              'Cincinnati', 'Monte Carlo', 'Shanghai', 'Paris'
          )
          AND tourney_level IN ('PM', 'P', 'W')
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]

    changed += _fix_numeric_wta_scraped_tour(local)

    local.execute("""
        UPDATE matches
        SET tourney_name =
            'W' || tourney_level || SUBSTR(
                tourney_name, LENGTH('M' || tourney_level) + 1)
        WHERE tourney_id = 'SCRAPED'
          AND tour = 'wta'
          AND tourney_level GLOB '[0-9]*'
          AND tourney_name LIKE 'M' || tourney_level || ' %'
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]

    local.execute("""
        DELETE FROM matches
        WHERE rowid IN (
            SELECT other.rowid
            FROM matches canonical
            JOIN matches other
              ON canonical.tourney_id = 'SCRAPED'
             AND other.tourney_id = 'SCRAPED'
             AND canonical.rowid != other.rowid
             AND canonical.round IN ('Q1', 'Q2')
             AND other.round NOT IN ('Q1', 'Q2')
             AND canonical.tour = other.tour
             AND SUBSTR(canonical.tourney_date, 1, 4) = SUBSTR(other.tourney_date, 1, 4)
             AND canonical.tourney_name = other.tourney_name
             AND canonical.winner_name = other.winner_name
             AND canonical.loser_name = other.loser_name
             AND COALESCE(canonical.score, '') = COALESCE(other.score, '')
             AND (
                LOWER(COALESCE(other.round, '')) IN (
                    'qualification', 'qualifications',
                    'qualification final', 'qualifying final',
                    'qualification round 1', 'qualifying round 1',
                    'qualification round 2', 'qualifying round 2'
                )
                OR (other.round = 'R1' AND canonical.round = 'Q1')
                OR (other.round = 'R2' AND canonical.round = 'Q2')
             )
        )
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]

    local.execute("""
        UPDATE matches
        SET round = CASE
            WHEN LOWER(COALESCE(round, '')) IN (
                'qualification final', 'qualifying final',
                'final qualifying round', 'qualification round 2',
                'qualifying round 2', 'qualification second round',
                'qualifying second round'
            ) THEN 'Q2'
            WHEN LOWER(COALESCE(round, '')) IN (
                'qualification', 'qualifications', 'qualifying',
                'qualification round 1', 'qualifying round 1',
                'qualification first round', 'qualifying first round'
            ) THEN 'Q1'
            ELSE round
        END
        WHERE tourney_id = 'SCRAPED'
          AND LOWER(COALESCE(round, '')) LIKE '%qual%'
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]

    local.execute("""
        DELETE FROM matches
        WHERE rowid IN (
            SELECT later.rowid
            FROM matches kept
            JOIN matches later
              ON kept.rowid < later.rowid
             AND kept.tourney_id = later.tourney_id
             AND kept.tourney_id = 'SCRAPED'
             AND later.tourney_id = 'SCRAPED'
             AND kept.tour = later.tour
             AND SUBSTR(kept.tourney_date, 1, 4) = SUBSTR(later.tourney_date, 1, 4)
             AND kept.tourney_name = later.tourney_name
             AND kept.round = later.round
             AND kept.winner_name = later.winner_name
             AND kept.loser_name = later.loser_name
        )
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]
    return changed


def sync_cloud_to_local(
    local_db_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[str, int], None]] = None,
    timeout_seconds: float = 60.0,
) -> dict:
    """Pull live data from Turso and merge into the local DB.

    Returns a dict with row counts per table.  Raises on connection failure.
    """
    from .db import _http_url, _auth_token
    import libsql_client

    if local_db_path is None:
        from tennis_app.core.data_manager import get_data_dir
        local_db_path = Path(get_data_dir()) / "tennis.db"
    local_db_path = Path(local_db_path)

    if not local_db_path.exists():
        raise FileNotFoundError(
            f"Local DB {local_db_path} does not exist. "
            "Run the app once to initialize the schema before syncing.")

    t0 = time.time()
    logger.info("Cloud sync: opening Turso connection...")
    client = libsql_client.create_client_sync(
        url=_http_url(), auth_token=_auth_token())

    local = sqlite3.connect(str(local_db_path))
    local.execute("PRAGMA busy_timeout=10000")

    counts: dict[str, int] = {}
    try:
        local.execute("BEGIN")

        # 1) matches: only the live (scraped) rows
        counts["matches"] = _copy_table(
            client, local, "matches",
            where="tourney_id='SCRAPED'",
            progress_callback=progress_callback,
        )
        # Remove SCRAPED rows that duplicate local CSV rows (same year +
        # tourney_name + round + winner + loser).  The cloud DB (Turso) has
        # no historical CSVs so it accumulates SCRAPED rows for matches that
        # are already in the local Sackmann archive — dedup here instead.
        n_dedup = 0
        local.execute("""
            DELETE FROM matches
            WHERE tourney_id = 'SCRAPED'
              AND rowid IN (
                SELECT scraped.rowid
                FROM matches scraped
                JOIN matches csv
                  ON SUBSTR(csv.tourney_date, 1, 4) = SUBSTR(scraped.tourney_date, 1, 4)
                 AND LOWER(csv.tourney_name) = LOWER(scraped.tourney_name)
                 AND csv.round = scraped.round
                 AND csv.winner_name = scraped.winner_name
                 AND csv.loser_name  = scraped.loser_name
                 AND csv.tourney_id != 'SCRAPED'
                WHERE scraped.tourney_id = 'SCRAPED'
              )
        """)
        n_dedup = local.execute("SELECT changes()").fetchone()[0]
        if n_dedup:
            logger.info("  matches: removed %d SCRAPED rows that duplicate CSV data",
                        n_dedup)
        n_canonical = _canonicalize_scraped_matches(local)
        if n_canonical:
            logger.info(
                "  matches: canonicalized/removed %d SCRAPED duplicate rows",
                n_canonical)

        # 2) rankings: only the live snapshot
        counts["rankings"] = _copy_table(
            client, local, "rankings",
            where="ranking_date='LIVE'",
            progress_callback=progress_callback,
        )

        # 3) players: upsert (preserves any CSV-loaded rows)
        for t in UPSERT_TABLES:
            counts[t] = _copy_table(
                client, local, t,
                upsert=True, delete_local=False,
                progress_callback=progress_callback,
            )

        # 4) full-replace tables (caches + extended-stats fact tables)
        for t in FULL_REPLACE_TABLES:
            counts[t] = _copy_table(
                client, local, t,
                progress_callback=progress_callback,
            )

        local.execute("COMMIT")
    except Exception:
        try:
            local.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        try:
            client.close()
        except Exception:
            pass
        local.close()

    elapsed = time.time() - t0
    total = sum(counts.values())
    logger.info("Cloud sync complete: %d rows in %.1fs", total, elapsed)
    counts["_elapsed_seconds"] = elapsed
    return counts


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    res = sync_cloud_to_local(
        progress_callback=lambda t, n: print(f"  {t}: {n} rows...", end="\r"))
    print()
    for k, v in res.items():
        print(f"{k}: {v}")
