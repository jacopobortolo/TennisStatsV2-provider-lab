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
