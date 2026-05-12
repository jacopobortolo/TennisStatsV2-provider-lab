"""One-time import of historical CSV data into Turso.

The scheduled cloud scrape job intentionally stores only live/scraped rows.
Run this script manually when the web app needs full historical leaderboards.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from tennis_app.core.data_manager import download_atp_data, download_wta_data

from .db import RemoteTennisDatabase


logger = logging.getLogger("cloud.import_historical")


def _progress(current: int, total: int, message: str) -> None:
    logger.info("[%d/%d] %s", current, total, message)


def _download_for_tour(tour: str, year_start: int, year_end: int, force: bool) -> None:
    logger.info("Downloading %s CSV cache for %d-%d", tour.upper(), year_start, year_end)
    if tour == "atp":
        download_atp_data(year_start=year_start, year_end=year_end, force=force, progress_callback=_progress)
    elif tour == "wta":
        download_wta_data(year_start=year_start, year_end=year_end, force=force, progress_callback=_progress)
    else:
        raise ValueError(f"Unsupported tour: {tour}")


def import_tour(tour: str, year_start: int, year_end: int, skip_download: bool, force_download: bool) -> None:
    if not skip_download:
        _download_for_tour(tour, year_start, year_end, force_download)

    logger.info("Importing %s historical CSV data into Turso", tour.upper())
    db = RemoteTennisDatabase()
    try:
        db.import_data(tour=tour, year_start=year_start, year_end=year_end, progress_callback=_progress)
    finally:
        db.close()
    logger.info("Finished %s historical import", tour.upper())


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import historical Sackmann CSV data into Turso")
    parser.add_argument("--tour", choices=["atp", "wta", "both"], default="both")
    parser.add_argument("--year-start", type=int, default=1968)
    parser.add_argument("--year-end", type=int, default=dt.date.today().year)
    parser.add_argument("--skip-download", action="store_true", help="Use the existing local CSV cache")
    parser.add_argument("--force-download", action="store_true", help="Re-download CSV files even if cache is fresh")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv or sys.argv[1:])
    tours = ["atp", "wta"] if args.tour == "both" else [args.tour]
    for tour in tours:
        import_tour(tour, args.year_start, args.year_end, args.skip_download, args.force_download)
    logger.info("Historical Turso import complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())