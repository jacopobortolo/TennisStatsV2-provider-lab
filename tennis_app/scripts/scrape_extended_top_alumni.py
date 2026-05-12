"""One-shot extended-stats scrape for every player who ever reached top-N.

Targets historical players (Federer, Nadal, Sharapova, etc.) whose
extended stats are missing because the regular live scrape only walks
the current top-N rankings.

Usage:
    python -m tennis_app.scripts.scrape_extended_top_alumni \
        --tours atp,wta --max-rank 20 --sleep 2.0

Idempotent within a run: existing fresh cache rows are skipped via the
normal ``scrape_player_extended_stats`` cache check (set ``--force`` to
override).  The script does NOT require a live ranking; it pulls the
target list from the ``rankings`` table history.
"""

import argparse
import logging
import sys
import time

from tennis_app.core.data_manager import scrape_player_extended_stats

logger = logging.getLogger(__name__)


def _collect_alumni(db, tour: str, max_rank: int):
    """Return a list of (player_id, full_name) ever ranked <= max_rank.

    Skips synthetic ``player_id`` markers used for live snapshots.
    """
    rows = db.conn.execute(
        """
        SELECT DISTINCT r.player_id,
               TRIM(COALESCE(p.name_first,'') || ' ' || COALESCE(p.name_last,'')) AS full_name
        FROM rankings r
        LEFT JOIN players p ON p.player_id = r.player_id
        WHERE r.tour = ?
          AND r.rank <= ?
          AND r.player_id NOT IN ('LIVE','SCRAPED_LIVE_SINGLES','SCRAPED_OFFICIAL_SINGLES')
        """,
        (tour, max_rank),
    ).fetchall()

    alumni = []
    for pid, name in rows:
        name = (name or "").strip()
        if not name:
            continue
        alumni.append((pid, name))
    # Sort alphabetically by surname for stable progress reporting.
    alumni.sort(key=lambda t: t[1].split()[-1].lower() if t[1] else "")
    return alumni


def _scrape_one(db, name: str, tour: str, force: bool):
    """Wrapper that swallows scraper exceptions and returns a status string."""
    try:
        result = scrape_player_extended_stats(
            name, db=db, tour=tour, force=force,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return f"error: {exc}"
    if not result:
        return "empty"
    total = sum(result.values())
    return f"{total} rows across {len(result)} tables"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--tours", default="atp,wta",
                        help="Comma-separated tours (default: atp,wta)")
    parser.add_argument("--max-rank", type=int, default=20,
                        help="Include players whose best DB rank is <= N "
                             "(default: 20)")
    parser.add_argument("--sleep", type=float, default=2.0,
                        help="Seconds to sleep between players (default: 2.0)")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape even if cache is fresh")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N players per tour (0 = no limit)")
    parser.add_argument("--cloud", action="store_true",
                        help="Write directly to Turso via RemoteTennisDatabase "
                             "instead of the local SQLite DB.")
    parser.add_argument("--players-file", default=None,
                        help="Path to a pipe-separated file produced by "
                             "dump_alumni_list.py (player_id|name|tour). "
                             "Required in --cloud mode because Turso has no "
                             "historical rankings.")
    args = parser.parse_args()

    tours = [t.strip().lower() for t in args.tours.split(",") if t.strip()]
    if args.cloud:
        from cloud.db import RemoteTennisDatabase
        db = RemoteTennisDatabase()
        logger.info("Using cloud (Turso) database")
    else:
        from tennis_app.core.database import TennisDatabase
        db = TennisDatabase()
        logger.info("Using local SQLite database")

    file_alumni: dict[str, list] = {}
    if args.players_file:
        from pathlib import Path
        path = Path(args.players_file)
        if not path.exists():
            logger.error("--players-file not found: %s", path)
            return 2
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            pid, name, tour = parts[0].strip(), parts[1].strip(), parts[2].strip().lower()
            if not name or tour not in ("atp", "wta"):
                continue
            file_alumni.setdefault(tour, []).append((pid, name))
        logger.info("Loaded alumni from file: %s",
                    {t: len(v) for t, v in file_alumni.items()})

    grand_total = 0
    for tour in tours:
        if tour not in ("atp", "wta"):
            logger.warning("Skipping unknown tour: %s", tour)
            continue

        if file_alumni:
            alumni = file_alumni.get(tour, [])
        else:
            alumni = _collect_alumni(db, tour, args.max_rank)
        if args.limit:
            alumni = alumni[: args.limit]
        logger.info("=== %s: %d players ever top-%d ===",
                    tour.upper(), len(alumni), args.max_rank)

        scraped = 0
        empty = 0
        errors = 0
        for idx, (pid, name) in enumerate(alumni, 1):
            status = _scrape_one(db, name, tour, args.force)
            logger.info("  [%d/%d] %s -> %s", idx, len(alumni), name, status)
            if status == "empty":
                empty += 1
            elif status.startswith("error"):
                errors += 1
            else:
                scraped += 1
                grand_total += 1
            if idx < len(alumni) and args.sleep > 0:
                time.sleep(args.sleep)

        logger.info(
            "=== %s done: %d with data, %d empty, %d errors ===",
            tour.upper(), scraped, empty, errors,
        )

    logger.info("All tours done. %d players with new extended-stats data.",
                grand_total)
    try:
        db.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
