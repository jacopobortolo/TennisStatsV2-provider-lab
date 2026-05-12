"""Diagnostic helper for the extended-stats scraping path.

Run with:

    python -m cloud.debug_extended "Joao Fonseca"            # ATP
    python -m cloud.debug_extended --tour wta "Linda Noskova"

What it does
------------
1. Resolves the player URL via tennisabstract conventions.
2. Downloads the jsfrags JS file (full data source) and saves it under
   ``./debug_extended/<Player>.jsfrags.js``.
3. Parses the embedded ``var player_frag = `...```` block and reports,
   per extended-stats table:
   - whether a ``<table id=...>`` was found at all
   - how many rows it contains
   - whether the ``var <table>_data = [...]`` JS array is also present
     in the file and how many entries it has
4. Calls ``fetch_all_extended_tables`` (the production path) and prints
   the per-table source (jsfrags vs cgi-fallback) and row counts so we
   can see whether the production path is leaving rows on the table.

Use this output to compare against the visible tables on
https://www.tennisabstract.com/cgi-bin/player.cgi?p=<UrlName> and decide
the right fix for issue 3.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

from tennis_app.core.scraper import (
    BASE_URL,
    TennisAbstractScraper,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("debug_extended")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("player", help="Full player name, e.g. 'Joao Fonseca'")
    parser.add_argument("--tour", choices=("atp", "wta"), default="atp")
    parser.add_argument(
        "--out-dir", default="debug_extended",
        help="Directory to drop the raw jsfrags file into.")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    scraper = TennisAbstractScraper()
    url_name = scraper._make_player_url_name(args.player)

    pid, jsfrags_text = scraper._discover_player_id(
        url_name, original_name=args.player, tour=args.tour)

    if not jsfrags_text:
        logger.error("Could not download jsfrags for %s (url_name=%s, tour=%s)",
                     args.player, url_name, args.tour)
        return 1

    raw_path = out_dir / f"{url_name}.jsfrags.js"
    raw_path.write_text(jsfrags_text, encoding="utf-8")
    logger.info("Saved raw jsfrags (%d bytes) to %s",
                len(jsfrags_text), raw_path)
    logger.info("Player profile URL: %s/cgi-bin/player.cgi?p=%s",
                BASE_URL, url_name)
    logger.info("Discovered player_id: %s", pid)

    # 1. Inspect player_frag block
    frag_match = re.search(
        r"var\s+player_frag\s*=\s*`(.*?)`", jsfrags_text, re.DOTALL)
    if not frag_match:
        logger.warning("No player_frag block found in jsfrags!")
    else:
        soup = BeautifulSoup(frag_match.group(1), "html.parser")
        all_tables = soup.find_all("table")
        logger.info("player_frag contains %d <table> elements (ids: %s)",
                    len(all_tables),
                    ", ".join(t.get("id") or "<no-id>" for t in all_tables))

    # 2. Per-table inspection: <table id=...> rows AND var <table>_data length
    print("\n--- per-table inspection ---")
    print(f"{'table':<18} {'frag-rows':>10} {'js-array-len':>14}")
    print("-" * 46)
    for table_name in TennisAbstractScraper.EXTENDED_TABLES:
        frag_rows = "n/a"
        if frag_match:
            soup = BeautifulSoup(frag_match.group(1), "html.parser")
            tbl = soup.find("table", id=table_name)
            if tbl:
                tbody = tbl.find("tbody")
                if tbody:
                    frag_rows = len(tbody.find_all("tr"))
                else:
                    frag_rows = max(0, len(tbl.find_all("tr")) - 1)
            else:
                frag_rows = "missing"

        # Some tennisabstract pages ship a parallel JS array
        # like `var winners_errors = [...]` or `var winnersErrors = [...]`
        # Try a few naming conventions.
        snake = table_name.replace("-", "_")
        camel = "".join(
            seg.capitalize() if i else seg
            for i, seg in enumerate(table_name.split("-")))
        js_len = "n/a"
        for var_name in (snake, camel):
            arr_match = re.search(
                rf"var\s+{re.escape(var_name)}\s*=\s*\[(.*?)\]\s*;",
                jsfrags_text, re.DOTALL)
            if arr_match:
                # Cheap row-count: top-level commas at depth 0
                body = arr_match.group(1)
                depth = 0
                count = 1 if body.strip() else 0
                for ch in body:
                    if ch == "[":
                        depth += 1
                    elif ch == "]":
                        depth -= 1
                    elif ch == "," and depth == 0:
                        count += 1
                js_len = f"{count} ({var_name})"
                break

        print(f"{table_name:<18} {str(frag_rows):>10} {str(js_len):>14}")

    # 3. Production-path call (will trigger the [ext-diag] log lines)
    print("\n--- production fetch_all_extended_tables ---")
    results = scraper.fetch_all_extended_tables(
        args.player, tour=args.tour)
    print("\nResult counts:")
    for t, rows in results.items():
        print(f"  {t}: {len(rows)} rows")
    missing = [t for t in TennisAbstractScraper.EXTENDED_TABLES
               if t not in results]
    if missing:
        print(f"  MISSING: {', '.join(missing)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
