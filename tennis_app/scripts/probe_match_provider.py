"""Read-only probe for lab match providers.

Examples:
    python -m tennis_app.scripts.probe_match_provider --provider sofascore --name "Jannik Sinner" --tour atp
    python -m tennis_app.scripts.probe_match_provider --provider hybrid --name "Iga Swiatek" --tour wta --min-year 2026
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from tennis_app.core.match_providers import (
    SofaScoreMatchProvider,
    get_match_provider,
)

logger = logging.getLogger(__name__)


def _records(df, limit: int) -> list[dict]:
    if df is None or df.empty:
        return []
    cols = [
        "tourney_date", "tourney_name", "tourney_level", "surface",
        "round", "winner_name", "loser_name", "winner_rank",
        "loser_rank", "score", "minutes",
    ]
    available = [c for c in cols if c in df.columns]
    return df[available].head(limit).to_dict(orient="records")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Probe lab match providers")
    parser.add_argument("--provider", default="sofascore",
                        choices=["tennisabstract", "sofascore", "hybrid"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--tour", default="atp", choices=["atp", "wta"])
    parser.add_argument("--min-year", type=int, default=None)
    parser.add_argument("--max-matches", type=int, default=20)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dump-json", type=Path, default=None,
                        help="Optional path for raw SofaScore candidates/events")
    parser.add_argument("--sofascore-api-base-url", default=None,
                        help="Optional SofaScore-compatible FastAPI base URL")
    args = parser.parse_args(argv)

    if args.sofascore_api_base_url:
        os.environ["SOFASCORE_API_BASE_URL"] = args.sofascore_api_base_url

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    provider = get_match_provider(args.provider)
    print(f"Provider: {args.provider}")
    print(f"Player: {args.name} ({args.tour.upper()})")

    raw_payload = None
    if args.provider == "sofascore":
        sofa = provider if isinstance(provider, SofaScoreMatchProvider) else SofaScoreMatchProvider()
        player, events = sofa.fetch_player_events(args.name, tour=args.tour, pages=1)
        raw_payload = {"player": player, "events": events}
        print(f"SofaScore entity: {(player or {}).get('name')} id={(player or {}).get('id')}")
        print(f"Raw events: {len(events)}")

    result = provider.fetch_player_matches(
        args.name, min_year=args.min_year, tour=args.tour,
        max_matches=args.max_matches,
    )
    df = result.df
    print(f"Provider result: {result.provider}")
    print(f"Rows: {0 if df is None else len(df)} latest={result.last_match_date}")
    for row in _records(df, args.limit):
        print(json.dumps(row, ensure_ascii=False, default=str))

    if args.dump_json and raw_payload is not None:
        args.dump_json.parent.mkdir(parents=True, exist_ok=True)
        args.dump_json.write_text(
            json.dumps(raw_payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"Wrote {args.dump_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())