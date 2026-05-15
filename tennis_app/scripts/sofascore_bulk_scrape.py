"""One-shot SofaScore match scrape for the top-N ranked players,
ignoring the scrape cache.

Requires the SofaScore local proxy to be running:
    python -m tennis_app.scripts.sofascore_proxy

Usage:
    set SOFASCORE_API_BASE_URL=http://127.0.0.1:8765/api/v1
    python -m tennis_app.scripts.sofascore_bulk_scrape \
        --count 50 --tour atp --cloud --sleep-max 3.0
"""

import argparse
import logging
import os
import random
import sys
import time

# Point SofaScoreMatchProvider at the local proxy (must be running).
os.environ.setdefault(
    "SOFASCORE_API_BASE_URL",
    os.getenv("SOFASCORE_PROXY_URL", "http://127.0.0.1:8765/api/v1"),
)

logger = logging.getLogger(__name__)


def _resolve_player(db, ranking_name: str, tour: str):
    """Resolve a live ranking display name to the canonical DB full name."""
    ranking_name = ranking_name.strip()
    if not ranking_name:
        return None
    norm = _normalize_name(ranking_name)
    # Exact DB match
    rows = db.conn.execute(
        "SELECT name_first, name_last FROM players WHERE tour = ?",
        (tour,),
    ).fetchall()
    for r in rows:
        first, last = r[0] or "", r[1] or ""
        full = f"{first} {last}".strip()
        if _normalize_name(full) == norm:
            return full
    # Prefix match: ranking name is a prefix of the DB full name
    for r in rows:
        first, last = r[0] or "", r[1] or ""
        full = f"{first} {last}".strip()
        if _normalize_name(full).startswith(norm + " "):
            return full
    # Return as-is and let the scraper try
    return ranking_name


def _normalize_name(name):
    import unicodedata
    import re
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.replace("-", " ")
    return re.sub(r"\s+", " ", name).strip().lower()


def _existing_match_keys(db, player_name: str, tour: str):
    """Return existing completed match keys for this player from any provider."""
    from tennis_app.core.match_providers import SofaScoreMatchProvider
    rows = db.conn.execute(
        """
        SELECT tour, tourney_date, winner_name, loser_name, tourney_name, round
        FROM matches
        WHERE tour = ?
          AND (winner_name = ? OR loser_name = ?)
          AND (is_upcoming = 0 OR is_upcoming IS NULL)
        """,
        (tour, player_name, player_name),
    ).fetchall()
    return {
        SofaScoreMatchProvider.match_key(
            row[0], row[1], row[2], row[3], row[4], row[5]
        )
        for row in rows
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Scrape SofaScore matches for the top-N ranked players.")
    parser.add_argument("--count", type=int, default=50,
                        help="Number of top-ranked players to scrape (default: 50).")
    parser.add_argument("--tour", default="atp", choices=["atp", "wta"])
    parser.add_argument("--cloud", action="store_true",
                        help="Write to Turso instead of the local DB.")
    parser.add_argument("--sleep", type=float, default=None,
                        help="Deprecated alias for --sleep-max.")
    parser.add_argument("--sleep-max", type=float, default=3.0,
                        help="Maximum random seconds to wait between players and HTTP requests (default: 3.0).")
    parser.add_argument("--stats-all", action="store_true",
                        help="Fetch statistics for existing matches too (default: only new matches).")
    parser.add_argument("--403-breaker", dest="breaker_403", type=int, default=5,
                        help="Stop after this many consecutive SofaScore 403 responses (default: 5).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print player names only, do not scrape.")
    parser.add_argument("--proxy-port", type=int, default=8765,
                        help="Port for the local SofaScore proxy (default: 8765).")
    parser.add_argument("--no-start-proxy", action="store_true",
                        help="Do not auto-start the local proxy.")
    args = parser.parse_args()

    proxy_url = f"http://127.0.0.1:{args.proxy_port}/api/v1"
    os.environ["SOFASCORE_API_BASE_URL"] = proxy_url
    sleep_max = args.sleep if args.sleep is not None else args.sleep_max
    sleep_max = max(0.0, min(3.0, float(sleep_max or 0.0)))
    os.environ["SOFASCORE_REQUEST_SLEEP_MAX"] = str(sleep_max)
    os.environ["SOFASCORE_403_BREAKER"] = str(max(1, int(args.breaker_403)))

    # Auto-start the local SofaScore proxy if needed
    proxy_process = None
    if not args.no_start_proxy:
        import subprocess
        import urllib.request
        health_url = f"http://127.0.0.1:{args.proxy_port}/health"
        try:
            urllib.request.urlopen(health_url, timeout=2)
            logger.info("SofaScore proxy already running on port %d.", args.proxy_port)
        except Exception:
            logger.info("Starting SofaScore proxy on port %d...", args.proxy_port)
            proxy_process = subprocess.Popen(
                [sys.executable, "-u", "-m", "tennis_app.scripts.sofascore_proxy",
                 "--host", "127.0.0.1", "--port", str(args.proxy_port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Wait for the proxy to be ready (can take a few seconds for uvicorn)
            for attempt in range(30):
                time.sleep(1.0)
                try:
                    resp = urllib.request.urlopen(health_url, timeout=2)
                    if resp.status == 200:
                        logger.info("SofaScore proxy is ready.")
                        break
                except Exception:
                    continue
            else:
                logger.error("SofaScore proxy did not start in time. "
                             "Check proxy output or start it manually "
                             "with run_sofascore_proxy.bat.")
                if proxy_process:
                    proxy_process.terminate()
                return 1

    if args.cloud:
        from cloud.db import RemoteTennisDatabase
        db = RemoteTennisDatabase()
        target = "Turso"
    else:
        from tennis_app.core.database import TennisDatabase
        db = TennisDatabase()
        target = "local"
    logger.info("Target DB: %s | Tour: %s | Top: %d",
                target, args.tour.upper(), args.count)

    # 1. Get current live rankings
    from tennis_app.core.data_manager import scrape_current_rankings
    logger.info("Fetching live rankings...")
    rankings = scrape_current_rankings(tour=args.tour)
    if not rankings:
        logger.error("No rankings returned - aborting.")
        return 1
    top_players = rankings[:args.count]
    logger.info("Will scrape %d players from SofaScore.", len(top_players))

    if args.dry_run:
        for entry in top_players:
            name = entry.get("name", "")
            resolved = _resolve_player(db, name, args.tour)
            print(f"  rank {entry.get('rank','?')}: {name}  ->  {resolved}")
        return 0

    # 2. Scrape each player
    from tennis_app.core.data_manager import scrape_player_matches
    from tennis_app.core.match_providers import SofaScoreAccessBlocked
    total_imported = 0
    success = 0
    errors = 0
    skipped_existing = 0

    for idx, entry in enumerate(top_players, 1):
        ranking_name = entry.get("name", "").strip()
        resolved = _resolve_player(db, ranking_name, args.tour)
        rank = entry.get("rank", "?")
        logger.info("[%d/%d] rank %s %s%s",
                    idx, len(top_players), rank, resolved,
                    f" (from {ranking_name})" if resolved != ranking_name else "")

        try:
            existing_keys = set()
            if not args.stats_all:
                existing_keys = _existing_match_keys(db, resolved, args.tour)
                if existing_keys:
                    logger.info("  -> %d existing matches will be skipped", len(existing_keys))
            df, last_date, _sig = scrape_player_matches(
                resolved, tour=args.tour, match_provider="sofascore",
                existing_match_keys=existing_keys,
                skip_existing_matches=not args.stats_all)
            if df is not None and not df.empty:
                imported = db.import_scraped_matches(
                    df, scraped_player_names=[resolved],
                    replace_existing=False)
                total_imported += imported
                logger.info("  -> %d matches scraped, %d new rows imported "
                            "(latest=%s)", len(df), imported, last_date)
            else:
                skipped_existing += len(existing_keys)
                logger.info("  -> no matches returned")
            success += 1
        except SofaScoreAccessBlocked as exc:
            logger.error("  -> SofaScore access blocked: %s", exc)
            logger.error("Stopping early to avoid extending the block.")
            errors += 1
            break
        except Exception:
            logger.exception("  -> error scraping %s", resolved)
            errors += 1

        if idx < len(top_players):
            time.sleep(random.uniform(0.0, sleep_max))

    logger.info(
        "Done. %d players: %d OK, %d errors, %d total rows imported, "
        "%d existing-match keys skipped.",
        len(top_players), success, errors, total_imported, skipped_existing)
    if proxy_process:
        logger.info("Shutting down proxy...")
        proxy_process.terminate()
        try:
            proxy_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy_process.kill()
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
