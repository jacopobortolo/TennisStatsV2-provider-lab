"""Dump the alumni list (player_id|full_name|tour) for every player ever
ranked in the top-N of either tour, reading from the local SQLite DB.

The output file is consumed by the cloud backfill workflow, which cannot
run the same query on Turso because Turso only stores the LIVE ranking
snapshot.

Usage:
    python -m tennis_app.scripts.dump_alumni_list \
        --max-rank 20 \
        --output tennis_app/scripts/data/alumni_top20.txt
"""

import argparse
import sys
from pathlib import Path

from tennis_app.core.database import TennisDatabase


def _collect(db, tour: str, max_rank: int):
    rows = db.conn.execute(
        """
        SELECT DISTINCT r.player_id,
               TRIM(COALESCE(p.name_first,'') || ' '
                    || COALESCE(p.name_last,'')) AS full_name
        FROM rankings r
        LEFT JOIN players p ON p.player_id = r.player_id
        WHERE r.tour = ?
          AND r.rank <= ?
          AND r.player_id NOT IN ('LIVE','SCRAPED_LIVE_SINGLES','SCRAPED_OFFICIAL_SINGLES')
        """,
        (tour, max_rank),
    ).fetchall()
    out = []
    for pid, name in rows:
        name = (name or "").strip()
        if not name:
            continue
        out.append((pid, name))
    out.sort(key=lambda t: t[1].split()[-1].lower())
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-rank", type=int, default=20)
    p.add_argument("--output", required=True,
                   help="Path to write the pipe-separated list")
    args = p.parse_args()

    db = TennisDatabase()
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# alumni top-{args.max_rank}: player_id|full_name|tour\n")
        for tour in ("atp", "wta"):
            alumni = _collect(db, tour, args.max_rank)
            for pid, name in alumni:
                f.write(f"{pid}|{name}|{tour}\n")
            print(f"{tour}: {len(alumni)} players")
            total += len(alumni)
    print(f"Wrote {total} rows to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
