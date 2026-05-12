"""One-shot backfill of weekly ranking history (ATP + WTA, 2025+).

Usage:
    python -m tennis_app.scripts.backfill_rankings_history \
        --tours atp,wta --start 2025-01-06 --end 2026-04-20 --top 500

Idempotent: weeks already present in the DB are skipped.

ATP source : atptour.com (?dateWeek=YYYY-MM-DD&rankRange=1-N)
WTA source : tennisexplorer.com (?date=YYYY-MM-DD&page=N, 50 rows/page)
"""

import argparse
import logging
import sys
from datetime import date, timedelta

from tennis_app.core.database import TennisDatabase

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--tours", default="atp,wta",
                        help="Comma-separated tours (default: atp,wta)")
    parser.add_argument("--start", default="2025-01-06",
                        help="First Monday to fetch (YYYY-MM-DD)")
    parser.add_argument("--end", default=None,
                        help="Last Monday to fetch (default: today)")
    parser.add_argument("--top", type=int, default=500,
                        help="Top-N ranks per snapshot (default: 500)")
    parser.add_argument("--sleep", type=float, default=2.0,
                        help="Seconds between weeks (default: 2.0)")
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    end = end - timedelta(days=end.weekday())

    db = TennisDatabase()
    total_inserted = 0
    for tour in [t.strip().lower() for t in args.tours.split(",") if t.strip()]:
        logger.info("=== Backfilling %s rankings %s -> %s (top %d) ===",
                    tour.upper(), args.start, end.isoformat(), args.top)
        result = db.backfill_rankings_history(
            tour=tour,
            start_date=args.start,
            end_date=end,
            top_n=args.top,
            sleep_s=args.sleep,
            progress_callback=lambda c, t, m: logger.info(
                "  [%d/%d] %s", c, t, m),
        )
        ins = sum(result.values())
        total_inserted += ins
        logger.info("=== %s done: %d rows inserted across %d weeks ===",
                    tour.upper(), ins, len(result))

    logger.info("Backfill complete. %d rows inserted total.", total_inserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
