"""Manual one-shot: scrape a single player (matches + extended stats)
and write to the local DB or directly to Turso.

Usage:
    python -m tennis_app.scripts.scrape_single_player \
        --name "Rafael Jodar" --tour atp --cloud
"""

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True,
                        help="Player full name (as on tennisabstract).")
    parser.add_argument("--tour", default="atp", choices=["atp", "wta"])
    parser.add_argument("--cloud", action="store_true",
                        help="Write to Turso instead of the local DB.")
    parser.add_argument("--min-year", type=int, default=None,
                        help="Drop matches before this year (default: none).")
    parser.add_argument("--no-matches", action="store_true",
                        help="Skip the matches scrape.")
    parser.add_argument("--no-extended", action="store_true",
                        help="Skip the extended-stats scrape.")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape extended stats even if cache fresh.")
    parser.add_argument("--match-provider", default=None,
                        choices=["tennisabstract", "sofascore", "hybrid"],
                        help="Live match provider for this lab copy")
    args = parser.parse_args()

    if args.cloud:
        from cloud.db import RemoteTennisDatabase
        db = RemoteTennisDatabase()
        target = "Turso"
    else:
        from tennis_app.core.database import TennisDatabase
        db = TennisDatabase()
        target = "local"
    logger.info("Target DB: %s", target)

    rc = 0
    try:
        # 1. Matches
        if not args.no_matches:
            from tennis_app.core.data_manager import scrape_player_matches
            logger.info("Scraping matches for %s (%s)...", args.name, args.tour)
            df, last_date, _match_signature = scrape_player_matches(
                args.name, tour=args.tour, min_year=args.min_year,
                match_provider=args.match_provider)
            logger.info("  fetched %d match rows (latest=%s)",
                        len(df), last_date)
            if not df.empty:
                imported = db.import_scraped_matches(
                    df, scraped_player_names=[args.name],
                    replace_existing=True)
                logger.info("  imported %d new rows", imported)
            else:
                logger.warning("  no matches returned by scraper")

        # 2. Extended stats
        if not args.no_extended:
            from tennis_app.core.data_manager import (
                scrape_player_extended_stats,
            )
            logger.info("Scraping extended stats for %s (%s)...",
                        args.name, args.tour)
            try:
                result = scrape_player_extended_stats(
                    args.name, db=db, tour=args.tour, force=args.force)
            except Exception:
                logger.exception("Extended-stats scrape failed")
                rc = 1
                result = None
            if result:
                total = sum(result.values())
                logger.info("  wrote %d rows across %d tables",
                            total, len(result))
            else:
                logger.info("  no extended-stats data")
    finally:
        try:
            db.close()
        except Exception:
            pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
