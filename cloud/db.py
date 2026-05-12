"""
Cloud-mode database connector for TennisStatsV2.

Strategy
--------
Turso/libSQL is reached over HTTP via pure-Python ``libsql_client``
(no native compilation; works on Python 3.13 and inside GitHub Actions
out of the box).

Two flavours are provided:

* :class:`SnapshotTennisDatabase` (desktop)
  Downloads a fresh snapshot of every relevant table from Turso into a
  local SQLite file on startup, then serves all queries from that local
  file at full speed.  Background thread can re-snapshot periodically.

* :class:`RemoteTennisDatabase` (scrape job)
  Proxies every SQL statement directly to Turso via HTTP — slower per
  query, but writes are persisted on the remote DB which is the whole
  point of the cloud variant.

Configuration via environment variables (or ``cloud/.env``):
    TURSO_DATABASE_URL   libsql://<db>-<org>.turso.io
    TURSO_AUTH_TOKEN     long-lived auth token from `turso db tokens create`
    TURSO_LOCAL_PATH     optional, defaults to ~/.tennis_analytics/cloud.db
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# .env loader (no python-dotenv dependency required)
# ---------------------------------------------------------------------------

def _load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    # utf-8-sig strips a leading BOM if present (some Windows editors /
    # PowerShell ``Set-Content -Encoding utf8`` add one, which would
    # otherwise prefix the first key name with U+FEFF and silently break
    # ``os.environ.get("TURSO_DATABASE_URL")``).
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _http_url() -> str:
    """libsql:// → https:// (libsql-client wants the HTTP form)."""
    raw = os.environ.get("TURSO_DATABASE_URL", "").strip()
    if not raw:
        raise RuntimeError("TURSO_DATABASE_URL is not set")
    if raw.startswith("libsql://"):
        return "https://" + raw[len("libsql://"):]
    return raw


def _auth_token() -> str:
    tok = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("TURSO_AUTH_TOKEN is not set")
    return tok


def get_local_replica_path() -> Path:
    raw = os.environ.get("TURSO_LOCAL_PATH")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".tennis_analytics" / "cloud.db"


# ---------------------------------------------------------------------------
# Remote-only DB (used by the GH Actions scrape job)
# ---------------------------------------------------------------------------

class _RemoteResultAdapter:
    """Adapt libsql-client's ResultSet to the sqlite3.Cursor surface used here."""

    def __init__(self, result):
        self._rows = list(result.rows) if result is not None else []
        self._idx = 0

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return tuple(row)

    def fetchall(self):
        rest = [tuple(r) for r in self._rows[self._idx:]]
        self._idx = len(self._rows)
        return rest

    def __iter__(self):
        for r in self._rows[self._idx:]:
            yield tuple(r)
        self._idx = len(self._rows)


class RemoteConnection:
    """Subset of sqlite3.Connection that proxies to Turso via HTTP."""

    def __init__(self):
        import libsql_client
        self._client = libsql_client.create_client_sync(
            url=_http_url(), auth_token=_auth_token())
        self.row_factory = None  # ignored; rows are tuples

    def execute(self, sql, params=()):
        _RETRYABLE = ("server disconnected", "connection reset", "timed out",
                      "connection refused", "temporarily unavailable")
        last_exc = None
        for attempt in range(4):
            try:
                if params:
                    result = self._client.execute(sql, list(params))
                else:
                    result = self._client.execute(sql)
                return _RemoteResultAdapter(result)
            except KeyError:
                raise sqlite3.OperationalError("remote SQL failed (libsql client)")
            except Exception as exc:
                msg = str(exc).lower()
                if any(k in msg for k in _RETRYABLE):
                    last_exc = exc
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(
                        "Turso transient error (attempt %d/4): %s — retrying in %ds",
                        attempt + 1, exc, wait,
                    )
                    time.sleep(wait)
                    # Re-create the client so the broken TCP session is discarded
                    try:
                        self._client.close()
                    except Exception:
                        pass
                    import libsql_client
                    self._client = libsql_client.create_client_sync(
                        url=_http_url(), auth_token=_auth_token())
                else:
                    raise sqlite3.OperationalError(str(exc)) from exc
        raise sqlite3.OperationalError(str(last_exc)) from last_exc

    def cursor(self):
        # TennisDatabase code calls conn.cursor().executescript(...) etc.
        # Our RemoteConnection already exposes execute/executemany/
        # executescript directly, so we can just return self.
        return self

    def executemany(self, sql, seq_of_params):
        statements = [(sql, list(p)) for p in seq_of_params]
        if not statements:
            return _RemoteResultAdapter(None)
        self._client.batch(statements)
        return _RemoteResultAdapter(None)

    def executescript(self, script):
        statements = [s.strip() for s in script.split(";") if s.strip()]
        if statements:
            self._client.batch(statements)

    def commit(self):
        # libsql-client commits per call; nothing to do here.
        pass

    def rollback(self):
        pass

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass


def _build_tennis_db(conn):
    """Construct a TennisDatabase wrapping an arbitrary connection,
    bypassing __init__ (which would create a local file)."""
    from tennis_app.core import database as _db
    impl = object.__new__(_db.TennisDatabase)
    impl.db_path = get_local_replica_path()
    impl.conn = conn
    impl._write_lock = threading.RLock()
    return impl


class RemoteTennisDatabase:
    """``TennisDatabase`` whose every SQL call hits Turso over HTTP."""

    _patched = False

    def __init__(self):
        self._conn_obj = RemoteConnection()
        self._impl = _build_tennis_db(self._conn_obj)
        try:
            self._impl._create_tables()
        except Exception as exc:
            logger.warning("Remote schema init failed: %s", exc)
        # Patch pandas.DataFrame.to_sql so import_scraped_matches /
        # import_extended_stats work against our HTTP-backed connection.
        if not RemoteTennisDatabase._patched:
            _install_pandas_to_sql_shim()
            RemoteTennisDatabase._patched = True

    def __getattr__(self, item):
        return getattr(self._impl, item)

    def close(self):
        self._conn_obj.close()


def _install_pandas_to_sql_shim():
    """Replace ``DataFrame.to_sql`` with a version that knows about
    :class:`RemoteConnection`.  Falls through to the original for normal
    sqlite3/SQLAlchemy connections."""
    import pandas as pd

    orig_to_sql = pd.DataFrame.to_sql

    def patched_to_sql(self, name, con, if_exists="fail", index=True,
                       index_label=None, chunksize=None, dtype=None,
                       method=None, **kwargs):
        if not isinstance(con, RemoteConnection):
            return orig_to_sql(self, name, con, if_exists=if_exists,
                               index=index, index_label=index_label,
                               chunksize=chunksize, dtype=dtype,
                               method=method, **kwargs)

        # ----- Remote path: emit DDL + batched INSERTs ourselves -----
        if if_exists == "replace":
            con.execute(f"DROP TABLE IF EXISTS {name}")
        # Build a CREATE TABLE statement from dtypes
        cols_ddl = []
        for col, dt in self.dtypes.items():
            sql_type = _pandas_dtype_to_sql(dt)
            cols_ddl.append(f'"{col}" {sql_type}')
        if if_exists in ("fail", "replace"):
            con.execute(f"CREATE TABLE IF NOT EXISTS {name} ({', '.join(cols_ddl)})")

        if self.empty:
            return 0

        cols_csv = ", ".join(f'"{c}"' for c in self.columns)
        placeholders = ", ".join("?" for _ in self.columns)
        insert_sql = f"INSERT INTO {name} ({cols_csv}) VALUES ({placeholders})"

        # libsql-client batches have a payload size limit; chunk to be safe.
        chunk = chunksize or 500
        rows = self.where(pd.notna(self), None).to_records(index=False)
        rows = [tuple(_normalize_value(v) for v in r) for r in rows]
        for i in range(0, len(rows), chunk):
            con.executemany(insert_sql, rows[i:i + chunk])
        return len(rows)

    pd.DataFrame.to_sql = patched_to_sql


def _pandas_dtype_to_sql(dt):
    s = str(dt)
    if "int" in s:
        return "INTEGER"
    if "float" in s:
        return "REAL"
    if "bool" in s:
        return "INTEGER"
    if "datetime" in s:
        return "TEXT"
    return "TEXT"


def _normalize_value(v):
    """Convert numpy scalars / pandas NaT to plain Python types."""
    if v is None:
        return None
    # numpy / pandas NA handling
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):  # numpy scalar
        try:
            return v.item()
        except Exception:
            return v
    return v


# ---------------------------------------------------------------------------
# Snapshot-based DB (used by the desktop client)
# ---------------------------------------------------------------------------

# Tables to mirror locally on the desktop client.  We fetch only these
# (plus their schema) to keep the snapshot small.
SNAPSHOT_TABLES = (
    "players", "matches", "rankings", "doubles_matches",
    "scrape_cache", "extended_stats_cache",
    "match_winners_errors", "match_serve_speed", "match_pbp_stats",
    "match_mcp_serve", "match_mcp_return", "match_mcp_rally",
    "match_mcp_tactics",
)


def download_snapshot(dest: Optional[Path] = None,
                      progress_callback=None) -> Path:
    """Download all rows of :data:`SNAPSHOT_TABLES` from Turso into *dest*.

    Returns the path of the local SQLite file.  Existing file is replaced
    atomically.
    """
    import libsql_client

    dest = Path(dest) if dest else get_local_replica_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.db")
    if tmp.exists():
        tmp.unlink()

    client = libsql_client.create_client_sync(
        url=_http_url(), auth_token=_auth_token())

    try:
        local = sqlite3.connect(str(tmp))
        local.execute("PRAGMA journal_mode=OFF")
        local.execute("PRAGMA synchronous=OFF")

        rs = client.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        remote_tables = {r[0]: r[1] for r in rs.rows}
        wanted = [t for t in SNAPSHOT_TABLES if t in remote_tables]
        logger.info("Snapshot: %d tables to copy from remote", len(wanted))

        for ti, tname in enumerate(wanted):
            ddl = remote_tables[tname]
            local.execute(ddl)
            page = 5000
            offset = 0
            total_rows = 0
            while True:
                rs = client.execute(
                    f"SELECT * FROM {tname} LIMIT ? OFFSET ?",
                    [page, offset])
                rows = rs.rows
                if not rows or not rs.columns:
                    break
                cols_csv = ", ".join(rs.columns)
                placeholders = ", ".join("?" for _ in rs.columns)
                local.executemany(
                    f"INSERT INTO {tname} ({cols_csv}) VALUES ({placeholders})",
                    [tuple(r) for r in rows])
                total_rows += len(rows)
                offset += page
                if progress_callback:
                    progress_callback(ti, len(wanted),
                                      f"{tname}: {total_rows} rows")
                if len(rows) < page:
                    break
            logger.info("  %s: %d rows", tname, total_rows)

        local.commit()
        local.close()

        if dest.exists():
            dest.unlink()
        tmp.rename(dest)
    finally:
        client.close()

    return dest


class SnapshotTennisDatabase:
    """Local SQLite backed by a periodically-refreshed Turso snapshot.

    Use this in the desktop app: queries are fast (local SQLite) and
    the snapshot stays current via :meth:`refresh`.
    """

    def __init__(self, refresh_on_open: bool = True):
        path = get_local_replica_path()
        if refresh_on_open or not path.exists():
            try:
                logger.info("Downloading Turso snapshot to %s ...", path)
                download_snapshot(path)
            except Exception as exc:
                logger.warning("Snapshot refresh failed (%s); using cached file",
                               exc)
                if not path.exists():
                    raise

        # Build a regular TennisDatabase against the local snapshot file.
        # Override the data-dir lookup so TennisDatabase opens *this* file.
        from tennis_app.core.database import TennisDatabase
        from tennis_app.core import data_manager
        orig = data_manager.get_data_dir
        data_manager.get_data_dir = lambda: path.parent

        # The DB filename TennisDatabase opens is data_dir/'tennis.db', so
        # ensure our snapshot file is named that on disk.
        target = path.parent / "tennis.db"
        if path != target:
            if target.exists():
                target.unlink()
            path.replace(target)

        try:
            self._impl = TennisDatabase()
        finally:
            data_manager.get_data_dir = orig

    def __getattr__(self, item):
        return getattr(self._impl, item)

    def refresh(self):
        """Re-download the snapshot.  Caller must close the DB first."""
        self._impl.close()
        path = get_local_replica_path()
        download_snapshot(path)
        from tennis_app.core.database import TennisDatabase
        from tennis_app.core import data_manager
        orig = data_manager.get_data_dir
        data_manager.get_data_dir = lambda: path.parent
        try:
            self._impl = TennisDatabase()
        finally:
            data_manager.get_data_dir = orig

    def close(self):
        self._impl.close()
