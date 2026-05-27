"""Restore Turso SCRAPED matches from a saved backup run.

Usage:
    python -m cloud.restore_scraped_backup --list
    python -m cloud.restore_scraped_backup --latest --yes
    python -m cloud.restore_scraped_backup --backup-run-id 20260527T101500Z --yes
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger("cloud.restore_scraped_backup")


def _list_backup_runs(db, limit: int) -> list[tuple]:
    return db.conn.execute(
        "SELECT backup_run_id, created_at, match_provider, tours, top_n, row_count "
        "FROM scraped_match_backup_runs "
        "ORDER BY created_at DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()


def _resolve_backup_run_id(db, backup_run_id: str | None, use_latest: bool):
    if backup_run_id:
        row = db.conn.execute(
            "SELECT backup_run_id, created_at, row_count "
            "FROM scraped_match_backup_runs WHERE backup_run_id = ?",
            (backup_run_id,),
        ).fetchone()
        return row
    if not use_latest:
        return None
    return db.conn.execute(
        "SELECT backup_run_id, created_at, row_count "
        "FROM scraped_match_backup_runs "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()


def _restore_backup(db, backup_run_id: str):
    from .scrape_job import _backup_scraped_matches, _ensure_scraped_backup_tables

    _ensure_scraped_backup_tables(db)
    current_snapshot_id, current_rows = _backup_scraped_matches(
        db,
        tours=("restore",),
        match_provider="manual-restore",
        top_n=0,
        min_year=None,
        max_matches_per_player=None,
    )
    logger.info(
        "Safety snapshot created before restore: run_id=%s rows=%d",
        current_snapshot_id,
        current_rows,
    )

    backup_rows = db.conn.execute(
        "SELECT COUNT(*) FROM matches_scraped_backup WHERE backup_run_id = ?",
        (backup_run_id,),
    ).fetchone()[0]
    if backup_rows <= 0:
        raise RuntimeError(f"Backup run {backup_run_id} contains no rows")

    match_cols = [
        row[1] for row in db.conn.execute("PRAGMA table_info(matches)").fetchall()
    ]
    cols_csv = ", ".join(match_cols)
    db.conn.execute("DELETE FROM matches WHERE tourney_id = 'SCRAPED'")
    db.conn.execute(
        f"INSERT INTO matches ({cols_csv}) "
        f"SELECT {cols_csv} FROM matches_scraped_backup WHERE backup_run_id = ?",
        (backup_run_id,),
    )
    db.conn.commit()
    return current_snapshot_id, current_rows, int(backup_rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore Turso SCRAPED matches from a saved backup run"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List recent SCRAPED backup runs and exit",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="How many backup runs to show with --list (default 10)",
    )
    parser.add_argument(
        "--backup-run-id",
        default=None,
        help="Exact backup_run_id to restore",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Restore the most recent backup run",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually perform the restore; without this the command only reports what it would do",
    )
    args = parser.parse_args(argv)

    if args.backup_run_id and args.latest:
        parser.error("Use either --backup-run-id or --latest, not both")
    if not args.list and not args.backup_run_id and not args.latest:
        args.list = True

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from .db import RemoteTennisDatabase
    from .scrape_job import _ensure_scraped_backup_tables

    db = RemoteTennisDatabase()
    try:
        _ensure_scraped_backup_tables(db)

        if args.list:
            rows = _list_backup_runs(db, max(1, int(args.limit)))
            if not rows:
                logger.info("No SCRAPED backup runs found")
                return 0
            for backup_run_id, created_at, provider, tours, top_n, row_count in rows:
                logger.info(
                    "backup_run_id=%s created_at=%s provider=%s tours=%s top_n=%s rows=%s",
                    backup_run_id,
                    created_at,
                    provider or "",
                    tours or "",
                    top_n if top_n is not None else "",
                    row_count,
                )
            return 0

        selected = _resolve_backup_run_id(db, args.backup_run_id, args.latest)
        if not selected:
            logger.error("Requested backup run not found")
            return 1
        backup_run_id, created_at, row_count = selected
        logger.info(
            "Selected backup_run_id=%s created_at=%s rows=%s",
            backup_run_id,
            created_at,
            row_count,
        )

        if not args.yes:
            logger.warning(
                "Dry run only. Re-run with --yes to restore this backup into matches"
            )
            return 0

        safety_snapshot_id, safety_rows, restored_rows = _restore_backup(
            db, backup_run_id
        )
        logger.info(
            "Restore complete: restored %d SCRAPED rows from %s; pre-restore snapshot=%s (%d rows)",
            restored_rows,
            backup_run_id,
            safety_snapshot_id,
            safety_rows,
        )
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())