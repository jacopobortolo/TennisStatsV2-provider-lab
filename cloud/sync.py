"""
Cloud → local sync.

Downloads the live data from Turso and merges it into the local SQLite
database (the one with the historical Sackmann CSVs).

Strategy per table:
    matches        : DELETE WHERE tourney_id='SCRAPED'  → bulk INSERT cloud rows
    rankings       : DELETE WHERE ranking_date='LIVE'   → bulk INSERT cloud rows
    players        : INSERT OR REPLACE (PK player_id+tour)
    scrape_cache / scrape_cache_provider : DELETE * → INSERT cloud rows
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

_RETRYABLE_REMOTE_ERRORS = (
    "server disconnected",
    "connection reset",
    "timed out",
    "connection refused",
    "temporarily unavailable",
    "clientoserror",
    "connection aborted",
    "network name is no longer available",
    "connection was aborted",
    "winerror 1236",
    "winerror 121",
    "semaphore timeout period has expired",
    "periodo di timeout del semaforo scaduto",
    "connessione in rete terminata dal sistema locale",
)


# Tables whose contents are *entirely* scraped (no CSV-historical rows).
# We mirror them by full-replace.  Cache tables are kept separate so callers
# that only refreshed recent match data can skip the heavy extended-stat facts.
CACHE_REPLACE_TABLES = (
    "scrape_cache",
    "scrape_cache_provider",
)

EXTENDED_REPLACE_TABLES = (
    "extended_stats_cache",
    "match_winners_errors",
    "match_serve_speed",
    "match_pbp_stats",
    "match_mcp_serve",
    "match_mcp_return",
    "match_mcp_rally",
    "match_mcp_tactics",
)

FULL_REPLACE_TABLES = CACHE_REPLACE_TABLES + EXTENDED_REPLACE_TABLES

# Tables with primary key — INSERT OR REPLACE is enough.
UPSERT_TABLES = (
    "players",  # PK (player_id, tour)
)


def _table_exists(local: sqlite3.Connection, name: str) -> bool:
    cur = local.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,))
    return cur.fetchone() is not None


def _remote_columns(client_ref, table: str) -> list[str]:
    rs = _remote_execute(client_ref, f"PRAGMA table_info({table})")
    return [r[1] for r in rs.rows]


def _local_columns(local: sqlite3.Connection, table: str) -> list[str]:
    cur = local.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def _remote_execute(client_ref, sql: str, params: Optional[list] = None):
    """Execute a Turso query with retry + client recreation on transient failures."""
    params = list(params or [])
    last_exc = None
    for attempt in range(4):
        try:
            client = client_ref["client"]
            if params:
                return client.execute(sql, params)
            return client.execute(sql)
        except Exception as exc:
            msg = str(exc).lower()
            if any(token in msg for token in _RETRYABLE_REMOTE_ERRORS):
                last_exc = exc
                wait = 2 ** attempt
                logger.warning(
                    "Turso sync transient error (attempt %d/4): %s — retrying in %ds",
                    attempt + 1, exc, wait,
                )
                time.sleep(wait)
                try:
                    client_ref["client"].close()
                except Exception:
                    pass
                client_ref["client"] = client_ref["factory"]()
                continue
            raise
    raise last_exc


def _stream_table_rows(
    client_ref,
    table: str,
    cols: list[str],
    where: Optional[str] = None,
    params: Optional[list] = None,
    page: int = 5000,
):
    """Yield remote table rows using keyset pagination over rowid."""
    params = list(params or [])
    col_csv = ", ".join(cols)
    where_sql = f"({where}) AND " if where else ""
    last_rowid = 0
    while True:
        paged_sql = (
            f"SELECT rowid, {col_csv} FROM {table} "
            f"WHERE {where_sql}rowid > ? "
            "ORDER BY rowid LIMIT ?"
        )
        rs = _remote_execute(client_ref, paged_sql, params + [last_rowid, page])
        if not rs.rows:
            return
        rows = [tuple(r) for r in rs.rows]
        last_rowid = rows[-1][0]
        yield cols, [r[1:] for r in rows]
        if len(rows) < page:
            return


def _copy_table(
    client_ref,
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

    remote_cols = _remote_columns(client_ref, table)
    if not remote_cols:
        logger.info("  %s: not present remotely, skipping", table)
        return 0
    local_cols = set(_local_columns(local, table))
    cols = [c for c in remote_cols if c in local_cols and c.lower() != "id"]
    if not cols:
        return 0

    sel_params: list = []

    if delete_local:
        if where:
            local.execute(f"DELETE FROM {table} WHERE {where}")
        else:
            local.execute(f"DELETE FROM {table}")

    verb = "INSERT OR REPLACE" if upsert else "INSERT"
    placeholders = ", ".join("?" for _ in cols)
    insert_sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"

    total = 0
    for _, page_rows in _stream_table_rows(client_ref, table, cols, where, sel_params):
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


def _normalize_wta_scraped_levels(local: sqlite3.Connection) -> int:
    from tennis_app.core.wta_tournament_levels import infer_wta_level_from_name

    rows = local.execute(
        """
        SELECT rowid, tourney_name, tourney_date, tourney_level
        FROM matches
        WHERE tourney_id = 'SCRAPED'
          AND tour = 'wta'
          AND COALESCE(tourney_name, '') != ''
        """
    ).fetchall()

    updates = []
    for rowid, tourney_name, tourney_date, tourney_level in rows:
        inferred_level = infer_wta_level_from_name(tourney_name, year=tourney_date)
        if inferred_level and inferred_level != tourney_level:
            updates.append((inferred_level, rowid))

    if not updates:
        return 0

    local.executemany(
        "UPDATE matches SET tourney_level = ? WHERE rowid = ?",
        updates,
    )
    return len(updates)


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

    n_wta_tour = _fix_numeric_wta_scraped_tour(local)
    if n_wta_tour:
        logger.info(
            "  matches: fixed tour field for %d scraped WTA match pairs "
            "(was 'atp', now 'wta')",
            n_wta_tour,
        )
    changed += n_wta_tour

    n_wta_levels = _normalize_wta_scraped_levels(local)
    if n_wta_levels:
        logger.info(
            "  matches: normalized %d scraped WTA main-tour levels",
            n_wta_levels,
        )
    changed += n_wta_levels

    local.execute("""
        UPDATE matches
        SET tourney_level = 'C'
        WHERE tourney_id = 'SCRAPED'
          AND LOWER(TRIM(COALESCE(tourney_name, ''))) LIKE '% ch'
          AND tourney_level != 'C'
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]

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
        UPDATE matches
        SET tourney_name = TRIM(SUBSTR(
            tourney_name, 1, LENGTH(tourney_name) - LENGTH(' Challenger'))
        ) || ' CH'
        WHERE tourney_id = 'SCRAPED'
          AND tour = 'atp'
          AND tourney_level = 'C'
          AND LOWER(tourney_name) LIKE '% challenger'
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]

    local.execute("""
        UPDATE matches
        SET tourney_name = tourney_name || ' CH'
        WHERE tourney_id = 'SCRAPED'
          AND tour = 'atp'
          AND tourney_level = 'C'
          AND LOWER(tourney_name) NOT LIKE '% ch'
          AND LOWER(tourney_name) NOT LIKE '% challenger'
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]

    local.execute("""
        UPDATE matches AS sf
        SET surface = (
            SELECT MIN(ref.surface)
            FROM matches ref
            WHERE ref.tourney_id = 'SCRAPED'
              AND ref.scrape_provider = 'tennisabstract'
              AND ref.tour = sf.tour
              AND SUBSTR(ref.tourney_date, 1, 4) = SUBSTR(sf.tourney_date, 1, 4)
              AND ref.tourney_name = sf.tourney_name
              AND COALESCE(ref.surface, '') != ''
        )
        WHERE sf.tourney_id = 'SCRAPED'
          AND sf.scrape_provider = 'sofascore'
          AND COALESCE(sf.surface, '') != ''
          AND (
            SELECT COUNT(DISTINCT ref.surface)
            FROM matches ref
            WHERE ref.tourney_id = 'SCRAPED'
              AND ref.scrape_provider = 'tennisabstract'
              AND ref.tour = sf.tour
              AND SUBSTR(ref.tourney_date, 1, 4) = SUBSTR(sf.tourney_date, 1, 4)
              AND ref.tourney_name = sf.tourney_name
              AND COALESCE(ref.surface, '') != ''
          ) = 1
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]

    local.execute("""
        DELETE FROM matches
        WHERE rowid IN (
            SELECT sf.rowid
            FROM matches sf
            JOIN matches ta
              ON ta.tourney_id = 'SCRAPED'
             AND sf.tourney_id = 'SCRAPED'
             AND ta.scrape_provider = 'tennisabstract'
             AND sf.scrape_provider = 'sofascore'
             AND ta.tour = sf.tour
             AND SUBSTR(ta.tourney_date, 1, 4) = SUBSTR(sf.tourney_date, 1, 4)
             AND ta.tourney_name = sf.tourney_name
             AND ta.round = sf.round
             AND ta.winner_name = sf.winner_name
             AND ta.loser_name = sf.loser_name
        )
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
        DELETE FROM matches
        WHERE rowid IN (
            SELECT sf.rowid
            FROM matches sf
            JOIN matches ta
              ON ta.tourney_id = 'SCRAPED'
             AND sf.tourney_id = 'SCRAPED'
             AND ta.scrape_provider = 'tennisabstract'
             AND sf.scrape_provider = 'sofascore'
             AND ta.tour = sf.tour
             AND SUBSTR(ta.tourney_date, 1, 4) = SUBSTR(sf.tourney_date, 1, 4)
             AND ta.winner_name = sf.winner_name
             AND ta.loser_name = sf.loser_name
             AND (
                (sf.round = 'R1' AND ta.round = 'Q1')
                OR (sf.round = 'R2' AND ta.round = 'Q2')
             )
             AND (
                COALESCE(ta.score, '') = COALESCE(sf.score, '')
                OR ta.score LIKE COALESCE(sf.score, '') || '%'
                OR sf.score LIKE COALESCE(ta.score, '') || '%'
             )
             AND ABS(
                CAST(COALESCE(NULLIF(sf.source_match_date, ''), sf.tourney_date) AS INTEGER)
                - CAST(ta.tourney_date AS INTEGER)
             ) <= 3
            WHERE ta.tourney_name = sf.tourney_name
               OR ta.tourney_level != sf.tourney_level
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
        UPDATE matches
        SET round = CASE round
            WHEN 'R1' THEN 'Q1'
            WHEN 'R2' THEN 'Q2'
            ELSE round
        END
        WHERE tourney_id = 'SCRAPED'
          AND scrape_provider = 'sofascore'
          AND round IN ('R1', 'R2')
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]

    local.execute("""
        UPDATE matches AS sf
        SET surface = (
            SELECT MIN(ref.surface)
            FROM matches ref
            WHERE ref.tourney_id = 'SCRAPED'
              AND ref.scrape_provider = 'tennisabstract'
              AND ref.tour = sf.tour
              AND ref.tourney_name = sf.tourney_name
              AND COALESCE(ref.surface, '') != ''
        )
        WHERE sf.tourney_id = 'SCRAPED'
          AND sf.scrape_provider = 'sofascore'
          AND (
            SELECT COUNT(DISTINCT ref.surface)
            FROM matches ref
            WHERE ref.tourney_id = 'SCRAPED'
              AND ref.scrape_provider = 'tennisabstract'
              AND ref.tour = sf.tour
              AND ref.tourney_name = sf.tourney_name
              AND COALESCE(ref.surface, '') != ''
          ) = 1
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

    local.execute("""
        DELETE FROM matches
        WHERE rowid IN (
            SELECT sf.rowid
            FROM matches sf
            JOIN matches ta
              ON ta.tourney_id = 'SCRAPED'
             AND sf.tourney_id = 'SCRAPED'
             AND ta.scrape_provider = 'tennisabstract'
             AND sf.scrape_provider = 'sofascore'
             AND ta.tour = sf.tour
             AND SUBSTR(ta.tourney_date, 1, 4) = SUBSTR(sf.tourney_date, 1, 4)
             AND ta.winner_name = sf.winner_name
             AND ta.loser_name = sf.loser_name
             AND ta.round = sf.round
             AND ta.tourney_name != sf.tourney_name
             AND ABS(
                CAST(COALESCE(NULLIF(sf.source_match_date, ''), sf.tourney_date) AS INTEGER)
                - CAST(ta.tourney_date AS INTEGER)
             ) <= 7
        )
    """)
    changed += local.execute("SELECT changes()").fetchone()[0]
    return changed


def sync_cloud_to_local(
    local_db_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[str, int], None]] = None,
    timeout_seconds: float = 60.0,
    match_provider: Optional[str] = None,
    include_players: bool = True,
    include_caches: bool = True,
    include_extended: bool = True,
    canonicalize_matches: bool = True,
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
    if match_provider or not include_players or not include_extended:
        logger.info(
            "Cloud sync scope: match_provider=%s, include_players=%s, "
            "include_caches=%s, include_extended=%s",
            match_provider or "all", include_players, include_caches,
            include_extended,
        )
    def _client_factory():
        return libsql_client.create_client_sync(
            url=_http_url(), auth_token=_auth_token())

    client_ref = {"client": _client_factory(), "factory": _client_factory}

    local = sqlite3.connect(str(local_db_path))
    local.execute("PRAGMA busy_timeout=10000")

    counts: dict[str, int] = {}
    try:
        local.execute("BEGIN")

        # 1) matches: only the live (scraped) rows.  A provider-scoped sync is
        # used by the SofaScore button because that job only changes SofaScore
        # rows; app startup still performs the full scraped-row mirror.
        matches_where = "tourney_id='SCRAPED'"
        if match_provider:
            provider = str(match_provider).strip().lower().replace("'", "''")
            matches_where += f" AND scrape_provider='{provider}'"
        counts["matches"] = _copy_table(
            client_ref, local, "matches",
            where=matches_where,
            progress_callback=progress_callback,
        )
        # Remove SCRAPED rows that duplicate local CSV rows (same year +
        # tourney_name + round + winner + loser).  The cloud DB (Turso) has
        # no historical CSVs so it accumulates SCRAPED rows for matches that
        # are already in the local Sackmann archive — dedup here instead.
        n_dedup = 0
        if canonicalize_matches:
            provider_filter = ""
            if match_provider:
                provider_filter = (
                    " AND scraped.scrape_provider = '"
                    + str(match_provider).strip().lower().replace("'", "''")
                    + "'"
                )
            local.execute(f"""
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
                  {provider_filter}
              )
        """)
            n_dedup = local.execute("SELECT changes()").fetchone()[0]
            if n_dedup:
                logger.info(
                    "  matches: removed %d SCRAPED rows that duplicate CSV data",
                    n_dedup)
            n_canonical = _canonicalize_scraped_matches(local)
            if n_canonical:
                logger.info(
                    "  matches: canonicalized/removed %d SCRAPED duplicate rows",
                    n_canonical)

        # 2) rankings: only the live snapshot
        counts["rankings"] = _copy_table(
            client_ref, local, "rankings",
            where="ranking_date='LIVE'",
            progress_callback=progress_callback,
        )

        # 3) players: upsert (preserves any CSV-loaded rows)
        if include_players:
            for t in UPSERT_TABLES:
                counts[t] = _copy_table(
                    client_ref, local, t,
                    upsert=True, delete_local=False,
                    progress_callback=progress_callback,
                )

        # 4) full-replace tables (caches + extended-stats fact tables)
        replace_tables = []
        if include_caches:
            replace_tables.extend(CACHE_REPLACE_TABLES)
        if include_extended:
            replace_tables.extend(EXTENDED_REPLACE_TABLES)
        for t in replace_tables:
            counts[t] = _copy_table(
                client_ref, local, t,
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
            client_ref["client"].close()
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
