"""
One-shot migration: copy every table (schema + rows) from a *source*
Turso database to a *destination* Turso database.

Usage
-----
1. Set environment variables (or pass via CLI flags):
     SRC_DATABASE_URL   libsql://<old-db>.turso.io
     SRC_AUTH_TOKEN     <token-for-old-db>
     DST_DATABASE_URL   libsql://<new-db>.turso.io
     DST_AUTH_TOKEN     <token-for-new-db>

2. Run:
     python -m cloud.migrate_db

Behaviour
---------
- For each table found in src ``sqlite_master`` (excluding ``sqlite_*``):
    * issues the CREATE TABLE on dst (IF NOT EXISTS),
    * paginates ``SELECT * FROM <table> LIMIT ? OFFSET ?`` in pages of 5000,
    * inserts via libsql ``batch()`` chunks of 200 rows.
- Skips ``_import_staging_*`` temp tables.
- Idempotent within a single run, but if interrupted the destination may
  contain partial data (TRUNCATE the dst table or drop+recreate the dst
  database before retrying for a clean slate).

Quota note
----------
Reads against the *source* DB consume the source account's row-read
quota.  If the source is throttled, the script will hang on the first
slow request — abort and start fresh on the dst instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Optional

logger = logging.getLogger("cloud.migrate_db")


def _http_url(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("libsql://"):
        return "https://" + raw[len("libsql://"):]
    return raw


def _client(url: str, token: str):
    import libsql_client
    return libsql_client.create_client_sync(
        url=_http_url(url), auth_token=token.strip())


def _list_tables(src) -> list[tuple[str, str]]:
    """Return list of (table_name, create_sql) from source, excluding
    sqlite internals and import staging tables."""
    rs = src.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "  AND name NOT LIKE '_import_staging_%'"
    )
    out = []
    for row in rs.rows:
        name, sql = row[0], row[1]
        if not sql:
            continue
        out.append((name, sql))
    return out


def _list_indexes(src) -> list[str]:
    """Return CREATE INDEX statements (excluding auto-indexes)."""
    rs = src.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='index' AND sql IS NOT NULL"
    )
    return [row[0] for row in rs.rows if row[0]]


def _row_count(src, table: str) -> Optional[int]:
    try:
        rs = src.execute(f"SELECT COUNT(*) FROM {table}")
        return rs.rows[0][0]
    except Exception as exc:
        logger.warning("COUNT(*) failed for %s: %s", table, exc)
        return None


def _copy_table(src, dst, table: str, page: int = 10000,
                batch: int = 500) -> int:
    total = _row_count(src, table)
    logger.info("→ %s: %s rows to copy",
                table, total if total is not None else "?")

    offset = 0
    copied = 0
    while True:
        t0 = time.time()
        rs = src.execute(
            f"SELECT * FROM {table} LIMIT ? OFFSET ?", [page, offset])
        rows = rs.rows
        cols = rs.columns
        if not rows:
            break

        cols_csv = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join("?" for _ in cols)
        insert_sql = f"INSERT INTO {table} ({cols_csv}) VALUES ({placeholders})"

        # libsql .batch() with chunks to stay under payload limit
        statements = []
        for r in rows:
            statements.append((insert_sql, [_norm(v) for v in r]))
            if len(statements) >= batch:
                dst.batch(statements)
                statements.clear()
        if statements:
            dst.batch(statements)

        copied += len(rows)
        offset += page
        dt = time.time() - t0
        logger.info("  %s: %d / %s rows  (page %.1fs)",
                    table, copied,
                    total if total is not None else "?", dt)

        if len(rows) < page:
            break

    return copied


def _norm(v):
    """Coerce row values into types libsql can serialize."""
    if v is None:
        return None
    # libsql_client passes through int/float/str/bool/bytes; everything
    # else becomes str.
    if isinstance(v, (int, float, str, bool, bytes)):
        return v
    return str(v)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--src-url", default=os.environ.get("SRC_DATABASE_URL"))
    p.add_argument("--src-token", default=os.environ.get("SRC_AUTH_TOKEN"))
    p.add_argument("--dst-url", default=os.environ.get("DST_DATABASE_URL"))
    p.add_argument("--dst-token", default=os.environ.get("DST_AUTH_TOKEN"))
    p.add_argument("--only", nargs="*", default=None,
                   help="Limit to these table names (default: all)")
    p.add_argument("--skip-data", action="store_true",
                   help="Create schema only, no row copy")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    missing = [k for k, v in [
        ("--src-url/SRC_DATABASE_URL", args.src_url),
        ("--src-token/SRC_AUTH_TOKEN", args.src_token),
        ("--dst-url/DST_DATABASE_URL", args.dst_url),
        ("--dst-token/DST_AUTH_TOKEN", args.dst_token),
    ] if not v]
    if missing:
        logger.error("Missing credentials: %s", ", ".join(missing))
        return 2

    logger.info("SRC: %s", args.src_url)
    logger.info("DST: %s", args.dst_url)

    src = _client(args.src_url, args.src_token)
    dst = _client(args.dst_url, args.dst_token)
    try:
        # 1) Schema
        tables = _list_tables(src)
        if args.only:
            wanted = set(args.only)
            tables = [(n, s) for n, s in tables if n in wanted]
        logger.info("Will copy %d tables: %s",
                    len(tables), ", ".join(n for n, _ in tables))

        for name, ddl in tables:
            # Re-shape "CREATE TABLE foo" → "CREATE TABLE IF NOT EXISTS foo"
            ddl_safe = ddl
            if ddl_safe.upper().startswith("CREATE TABLE ") \
                    and "IF NOT EXISTS" not in ddl_safe.upper():
                ddl_safe = ddl_safe.replace(
                    "CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1)
            try:
                dst.execute(ddl_safe)
            except Exception as exc:
                logger.warning("DDL failed for %s: %s", name, exc)

        # 2) Data
        if not args.skip_data:
            grand_total = 0
            for name, _ in tables:
                try:
                    grand_total += _copy_table(src, dst, name)
                except Exception:
                    logger.exception("Copy failed for %s — continuing", name)
            logger.info("Data copy done: %d total rows inserted", grand_total)

        # 3) Indexes (after data so inserts are faster)
        idx_stmts = _list_indexes(src)
        logger.info("Recreating %d indexes...", len(idx_stmts))
        for stmt in idx_stmts:
            stmt_safe = stmt
            if stmt_safe.upper().startswith("CREATE INDEX ") \
                    and "IF NOT EXISTS" not in stmt_safe.upper():
                stmt_safe = stmt_safe.replace(
                    "CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
            elif stmt_safe.upper().startswith("CREATE UNIQUE INDEX ") \
                    and "IF NOT EXISTS" not in stmt_safe.upper():
                stmt_safe = stmt_safe.replace(
                    "CREATE UNIQUE INDEX",
                    "CREATE UNIQUE INDEX IF NOT EXISTS", 1)
            try:
                dst.execute(stmt_safe)
            except Exception as exc:
                logger.warning("Index DDL failed: %s — %s", exc, stmt[:80])

        logger.info("Migration complete.")
        return 0
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
