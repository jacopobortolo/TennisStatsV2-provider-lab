"""
Headless scrape job — runs the same live-data pipeline as the UI's
"Scrape Live Data" button, but without launching Qt.

Schedule it on Windows with Task Scheduler (see ``scripts/install_task.ps1``)
to keep the local SQLite database fresh while the app is closed.

Usage:
    python -m tennis_app.cron               # singles + extended (top 150)
    python -m tennis_app.cron --top 500     # bigger pull
    python -m tennis_app.cron --no-extended # skip extended-stats step
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime

from .core.data_manager import (
    scrape_top_players_matches,
    scrape_top_players_extended_stats,
)
from .core.database import TennisDatabase

logger = logging.getLogger("tennis_app.cron")


def _print_progress(current, total, msg):
    pct = int(current / total * 100) if total else 0
    text = f"  [{pct:3d}%] {msg}"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, "replace").decode(encoding), flush=True)


def run(top_n: int = 150, do_extended: bool = True,
    min_year: int = 2025, match_provider: str | None = None) -> int:
    """Run a full scrape cycle. Returns total scraped matches."""
    started = time.time()
    db = TennisDatabase()
    total_new = 0

    try:
        for tour in ("atp", "wta"):
            logger.info("=== %s: scraping top %d ===", tour.upper(), top_n)
            matches_df, rankings, scraped_names = scrape_top_players_matches(
                top_n=top_n, tour=tour,
                progress_callback=_print_progress,
                db=db,
                cache_expire_hours=6,
                min_year=min_year,
                match_provider=match_provider,
            )
            if not matches_df.empty:
                new = db.import_scraped_matches(
                    matches_df, scraped_player_names=scraped_names,
                    replace_existing=False)
                total_new += new or 0
            if rankings:
                db.import_scraped_rankings(rankings)

        if do_extended:
            for tour in ("atp", "wta"):
                logger.info("=== %s: extended stats top %d ===",
                            tour.upper(), min(150, top_n))
                scrape_top_players_extended_stats(
                    top_n=min(150, top_n), tour=tour,
                    progress_callback=_print_progress,
                    db=db,
                )
    finally:
        db.close()

    elapsed = time.time() - started
    logger.info("Done in %.1fs — %d new matches", elapsed, total_new)
    return total_new


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Headless tennis scrape")
    parser.add_argument("--top", type=int, default=150,
                        help="Top-N players to refresh (default: 150)")
    parser.add_argument("--no-extended", action="store_true",
                        help="Skip the extended-stats pass")
    parser.add_argument("--min-year", type=int, default=2025,
                        help="Earliest match year to keep (default: 2025)")
    parser.add_argument("--monday-boost", action="store_true",
                        help="Use top-1000 on Mondays for fresh rank deltas")
    parser.add_argument("--match-provider", default=None,
                        choices=["tennisabstract", "sofascore", "hybrid"],
                        help="Live match provider for this lab copy")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    top_n = args.top
    if args.monday_boost and datetime.today().weekday() == 0:
        top_n = max(top_n, 1000)
        logger.info("Monday boost active: top_n=%d", top_n)

    try:
        run(top_n=top_n, do_extended=not args.no_extended,
            min_year=args.min_year, match_provider=args.match_provider)
        return 0
    except Exception:
        logger.exception("Cron run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
