"""
Cloud scrape job — runs inside GitHub Actions.

Strategy:
1. Open a libSQL connection to Turso (writable).
2. Pull the current schema/data into a local replica file.
3. Run the standard ``tennis_app.cron`` pipeline against the writable
   connection.  All inserts/updates are pushed to Turso.

This script is what the GitHub Actions workflow invokes hourly.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from datetime import datetime

import pandas as pd
import requests

logger = logging.getLogger("cloud.scrape_job")


PLAYERS_URLS = {
    "atp": "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_players.csv",
    "wta": "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_players.csv",
}


def _format_counts(counts):
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _log_tour_report(tour, match_report, extended_report=None):
    """Emit a compact end-of-tour scrape summary for GitHub Actions logs."""
    logger.info("--- %s scrape summary ---", tour.upper())
    logger.info(
        "rankings=%s source=%s stale=%s skipped=%s retry_cooldown=%s "
        "reasons=[%s]",
        match_report.get("rankings", 0),
        match_report.get("source") or "?",
        match_report.get("stale", 0),
        match_report.get("skipped", 0),
        match_report.get("retry_cooldown", 0),
        _format_counts(match_report.get("stale_reasons", {})),
    )
    logger.info(
        "activity statuses=[%s]",
        _format_counts(match_report.get("activity_statuses", {})),
    )
    logger.info(
        "matches attempted=%s confirmed=%s ta_lag=%s not_found=%s "
        "empty=%s errors=%s fetched_rows=%s imported_rows=%s",
        match_report.get("attempted", 0),
        match_report.get("confirmed", 0),
        match_report.get("ta_lag", 0),
        match_report.get("not_found", 0),
        match_report.get("empty", 0),
        match_report.get("errors", 0),
        match_report.get("rows", 0),
        match_report.get("imported_rows", 0),
    )
    if extended_report is None:
        logger.info("extended skipped")
        return
    logger.info(
        "extended candidates=%s selected=%s skipped_budget=%s "
        "reasons=[%s] activity_statuses=[%s] with_rows=%s empty=%s "
        "errors=%s rows=%s",
        extended_report.get("candidates", 0),
        extended_report.get("selected", 0),
        extended_report.get("skipped_budget", 0),
        _format_counts(extended_report.get("reasons", {})),
        _format_counts(extended_report.get("activity_statuses", {})),
        extended_report.get("scraped", 0),
        extended_report.get("empty", 0),
        extended_report.get("errors", 0),
        extended_report.get("rows", 0),
    )


def _seed_players_if_empty(db):
    """Populate Turso ``players`` table on first run.

    The scraper's name-resolution logic (``_resolve_ranking_name``) relies on
    ``players`` to expand truncated ranking names like 'Daniel Mérida' into
    the full 'Daniel Mérida Aguilar' that tennisabstract uses in its URLs.
    Without this the scrape silently misses any player whose ranking name
    is shorter than their CSV name.
    """
    try:
        rs = db.conn.execute("SELECT COUNT(*) FROM players")
        existing = rs.fetchone()[0]
    except Exception:
        existing = 0
    if existing and existing > 100:
        logger.info("players table already seeded (%d rows)", existing)
        return

    logger.info("Seeding players table from Sackmann CSVs...")
    for tour, url in PLAYERS_URLS.items():
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            df = pd.read_csv(
                io.StringIO(resp.text),
                dtype={"player_id": str, "dob": str, "wikidata_id": str},
                low_memory=False,
            )
            if df.empty:
                continue
            df["tour"] = tour
            # Use the to_sql shim installed by RemoteTennisDatabase
            df.to_sql("players", db.conn, if_exists="append",
                      index=False, chunksize=500)
            logger.info("  %s: seeded %d players", tour.upper(), len(df))
        except Exception:
            logger.exception("Failed to seed %s players", tour)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cloud scrape job (Turso)")
    parser.add_argument("--top", type=int, default=1000,
                        help="Top-N players to refresh per tour (default 1000)")
    parser.add_argument("--no-extended", action="store_true")
    parser.add_argument("--extended-budget", type=int, default=120,
                        help="Max extended-stat players per tour/run")
    parser.add_argument("--inactive-extended-budget", type=int, default=20,
                        help="Max inactive time-based extended refreshes per tour/run")
    parser.add_argument("--max-workers", type=int, default=8,
                        help="Max parallel match-provider fetches")
    parser.add_argument("--max-matches-per-player", type=int, default=20,
                        help="Max recent matches to fetch/import per player")
    parser.add_argument("--min-year", type=int, default=2025)
    parser.add_argument("--match-provider", default=None,
                        choices=["tennisabstract", "sofascore", "hybrid"],
                        help="Live match provider for this lab copy")
    parser.add_argument("--monday-boost", action="store_true",
                        help="(legacy, no-op — top is already full)")
    parser.add_argument("--seed-players-only", action="store_true",
                        help="Only seed the players table, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from .db import RemoteTennisDatabase
    from tennis_app.core.data_manager import (
        scrape_top_players_matches,
        scrape_top_players_extended_stats,
    )

    top_n = args.top
    logger.info(
        "Cloud scrape: top_n=%d per tour, extended_budget=%d, "
        "inactive_extended_budget=%d, max_workers=%d, "
        "max_matches_per_player=%d, match_provider=%s",
        top_n, args.extended_budget, args.inactive_extended_budget,
        args.max_workers, args.max_matches_per_player,
        args.match_provider or "env/default",
    )

    db = RemoteTennisDatabase()

    try:
        # Seed the players table on first run so name resolution works.
        _seed_players_if_empty(db)
        if args.seed_players_only:
            logger.info("Seed-only mode: done.")
            return 0
        # Cloud mode is "live-only": we don't import the 1.7M-row Sackmann
        # historical CSVs into Turso (would take hours via HTTP).  Use local
        # mode for full archive queries; cloud mode = always-fresh top-N.
        tour_payloads = {}
        for tour in ("atp", "wta"):
            logger.info("=== %s: scraping top %d ===", tour.upper(), top_n)
            scrape_result = scrape_top_players_matches(
                top_n=top_n, tour=tour,
                progress_callback=lambda c, t, m: logger.info(
                    "  [%d/%d] %s", c, t, m),
                db=db,
                cache_expire_hours=24,
                min_year=args.min_year,
                max_matches_per_player=args.max_matches_per_player,
                max_workers=args.max_workers,
                match_provider=args.match_provider,
                return_report=True,
            )
            matches_df, rankings, scraped_names, match_report = scrape_result
            imported = 0
            if not matches_df.empty:
                imported = db.import_scraped_matches(
                    matches_df, scraped_player_names=scraped_names,
                    replace_existing=False)
            match_report["imported_rows"] = imported
            if rankings:
                db.import_scraped_rankings(rankings)
            tour_payloads[tour] = {
                "rankings": rankings,
                "scraped_names": scraped_names,
                "match_report": match_report,
            }

        if not args.no_extended:
            for tour in ("atp", "wta"):
                logger.info("=== %s: extended stats (top %d) ===",
                            tour.upper(), top_n)
                payload = tour_payloads.get(tour, {})
                _, extended_report = scrape_top_players_extended_stats(
                    top_n=top_n, tour=tour,
                    progress_callback=lambda c, t, m: logger.info(
                        "  [%d/%d] %s", c, t, m),
                    db=db,
                    rankings=payload.get("rankings"),
                    priority_players=payload.get("scraped_names"),
                    budget=args.extended_budget,
                    inactive_budget=args.inactive_extended_budget,
                    return_report=True,
                )
                payload["extended_report"] = extended_report

        for tour in ("atp", "wta"):
            payload = tour_payloads.get(tour, {})
            _log_tour_report(
                tour,
                payload.get("match_report", {}),
                payload.get("extended_report"),
            )

        # Final commit (no-op for remote — every call already round-trips)
        logger.info("Cloud scrape complete")
    except Exception:
        logger.exception("Cloud scrape failed")
        return 1
    finally:
        try:
            db.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
