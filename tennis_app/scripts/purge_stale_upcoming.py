"""One-shot cleanup: delete is_upcoming=1 rows whose tourney_date is
older than today.  These are stale placeholder rows left over before
the canonicalize fix in import_scraped_matches.

Run with --cloud to clean Turso, or without to clean the local DB.

Usage:
    python -m tennis_app.scripts.purge_stale_upcoming --cloud
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
    p = argparse.ArgumentParser()
    p.add_argument("--cloud", action="store_true",
                   help="Clean the Turso database instead of the local one.")
    args = p.parse_args()

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y%m%d")

    if args.cloud:
        from cloud.db import RemoteTennisDatabase
        db = RemoteTennisDatabase()
        target = "Turso"
    else:
        from tennis_app.core.database import TennisDatabase
        db = TennisDatabase()
        target = "local"

    try:
        # Count first for logging
        before = db.conn.execute(
            "SELECT COUNT(*) FROM matches "
            "WHERE is_upcoming = 1 AND tourney_date < ?",
            (today,),
        ).fetchone()[0]
        logger.info("%s: %d stale is_upcoming rows (date < %s)",
                    target, before, today)

        if before == 0:
            logger.info("Nothing to do.")
            return 0

        db.conn.execute(
            "DELETE FROM matches "
            "WHERE is_upcoming = 1 AND tourney_date < ?",
            (today,),
        )
        db.conn.commit()
        logger.info("Deleted %d rows from %s.", before, target)
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
