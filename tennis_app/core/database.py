"""
SQLite database layer for fast querying of tennis data.
"""

import sqlite3
import logging
import threading
import unicodedata
import uuid
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .data_manager import (get_data_dir, load_players, load_matches,
                           load_rankings, load_doubles,
                           scrape_player_matches,
                           scrape_current_rankings, scrape_top_players_matches)

logger = logging.getLogger(__name__)


def _strip_diacritics(text):
    """Remove diacritical marks from a string (e.g. João → Joao)."""
    if not text or not isinstance(text, str):
        return text
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _player_match_key(name):
    """Canonical lookup key for player-name matching across CSV / scraped
    sources.  Lowercased, transliterated (ø → o, ł → l, …), apostrophe-free,
    hyphen-free, whitespace-collapsed.  Used to merge variants like::

        Christopher O'Connell ↔ Christopher Oconnell
        Elmer Møller          ↔ Elmer Moller
        Auger-Aliassime       ↔ Auger Aliassime
    """
    if not isinstance(name, str) or not name:
        return ""
    # Re-use the scraper's transliteration table (handles ø/ł/ı/ş/ğ/ß/…)
    # plus NFKD diacritic stripping.
    try:
        from .scraper import clean_player_name as _clean
        s = _clean(name) or name
    except Exception:
        s = _strip_diacritics(name)
    for ch in ("'", "\u2019", "\u2018", "`"):
        s = s.replace(ch, "")
    s = s.replace("-", " ")
    s = " ".join(s.split())
    return s.lower()


def _locked_write(method):
    """Decorator: serialize a write method through ``self._write_lock``.

    Required because the Qt UI shares a single SQLite connection across
    QThread workers (live scrape + background extended-stats), and SQLite
    write transactions are not concurrent-safe at the connection level
    even with WAL.
    """
    def wrapper(self, *args, **kwargs):
        with self._write_lock:
            return method(self, *args, **kwargs)
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


def get_db_path():
    return get_data_dir() / "tennis.db"


def _is_remote_conn(conn) -> bool:
    """True when ``conn`` is the Turso HTTP RemoteConnection.

    Detected by class name to avoid an import cycle with ``cloud.db``.
    On a remote connection, every ``execute`` is a slow HTTP roundtrip,
    so per-row migrations must be skipped (or batched into a single
    statement).
    """
    return type(conn).__name__ == "RemoteConnection"


def _eq_or_in(column, value, conditions, params):
    """Append a ``column = ?`` or ``column IN (?,?,…)`` filter.

    Accepts a scalar (single equality) or a list/tuple/set of values
    (turned into an ``IN`` clause).  Empty iterables are ignored.
    """
    if value is None:
        return
    if isinstance(value, (list, tuple, set)):
        vals = [v for v in value if v not in (None, "")]
        if not vals:
            return
        if len(vals) == 1:
            conditions.append(f"{column} = ?")
            params.append(vals[0])
        else:
            placeholders = ",".join("?" * len(vals))
            conditions.append(f"{column} IN ({placeholders})")
            params.extend(vals)
    else:
        conditions.append(f"{column} = ?")
        params.append(value)


class TennisDatabase:
    """SQLite-backed database for tennis analytics."""

    def __init__(self):
        self.db_path = get_db_path()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Re-entrant write lock to serialize writes across QThread workers
        # (live scrape worker + background extended-stats worker share the
        # same connection and would otherwise raise "database is locked").
        self._write_lock = threading.RLock()
        # In-memory result caches (keyed by (player_id, tour))
        self._career_stats_cache: dict = {}
        self._ranking_history_cache: dict = {}
        # Performance pragmas
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-64000")  # 64 MB cache
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA mmap_size=268435456")  # 256 MB mmap
        # Wait up to 10s for SQLite-level locks to clear before erroring
        self.conn.execute("PRAGMA busy_timeout=10000")
        self._create_tables()
        self._run_analyze_once()

    def _create_tables(self):
        cur = self.conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                player_id TEXT,
                name_first TEXT,
                name_last TEXT,
                hand TEXT,
                dob TEXT,
                ioc TEXT,
                height REAL,
                wikidata_id TEXT,
                tour TEXT,
                PRIMARY KEY (player_id, tour)
            );

            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tourney_id TEXT,
                tourney_name TEXT,
                surface TEXT,
                draw_size INTEGER,
                tourney_level TEXT,
                tourney_date TEXT,
                match_num INTEGER,
                winner_id TEXT,
                winner_seed TEXT,
                winner_entry TEXT,
                winner_name TEXT,
                winner_hand TEXT,
                winner_ht REAL,
                winner_ioc TEXT,
                winner_age REAL,
                loser_id TEXT,
                loser_seed TEXT,
                loser_entry TEXT,
                loser_name TEXT,
                loser_hand TEXT,
                loser_ht REAL,
                loser_ioc TEXT,
                loser_age REAL,
                score TEXT,
                best_of INTEGER,
                round TEXT,
                minutes REAL,
                w_ace REAL, w_df REAL, w_svpt REAL, w_1stIn REAL,
                w_1stWon REAL, w_2ndWon REAL, w_SvGms REAL,
                w_bpSaved REAL, w_bpFaced REAL,
                l_ace REAL, l_df REAL, l_svpt REAL, l_1stIn REAL,
                l_1stWon REAL, l_2ndWon REAL, l_SvGms REAL,
                l_bpSaved REAL, l_bpFaced REAL,
                winner_rank REAL, winner_rank_points REAL,
                loser_rank REAL, loser_rank_points REAL,
                tour TEXT,
                is_upcoming INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS rankings (
                ranking_date TEXT,
                rank INTEGER,
                player_id TEXT,
                points INTEGER,
                tour TEXT,
                age INTEGER,
                rank_diff INTEGER,
                pts_diff INTEGER,
                next_tournament TEXT,
                ioc TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_matches_winner ON matches(winner_id);
            CREATE INDEX IF NOT EXISTS idx_matches_loser ON matches(loser_id);
            CREATE INDEX IF NOT EXISTS idx_matches_tourney ON matches(tourney_name);
            CREATE INDEX IF NOT EXISTS idx_matches_surface ON matches(surface);
            CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(tourney_date);
            CREATE INDEX IF NOT EXISTS idx_matches_winner_name ON matches(winner_name);
            CREATE INDEX IF NOT EXISTS idx_matches_loser_name ON matches(loser_name);
            CREATE INDEX IF NOT EXISTS idx_rankings_player ON rankings(player_id);
            CREATE INDEX IF NOT EXISTS idx_rankings_tour_date ON rankings(tour, ranking_date);
            CREATE INDEX IF NOT EXISTS idx_matches_winner_name_tour ON matches(winner_name, tour);
            CREATE INDEX IF NOT EXISTS idx_matches_loser_name_tour ON matches(loser_name, tour);
            CREATE INDEX IF NOT EXISTS idx_players_name ON players(name_last, name_first);

            CREATE TABLE IF NOT EXISTS scrape_cache (
                player_name TEXT PRIMARY KEY,
                last_scraped TEXT NOT NULL,
                match_count INTEGER DEFAULT 0,
                last_match_date TEXT,
                activity_fingerprint TEXT,
                match_signature TEXT,
                scrape_retry_count INTEGER DEFAULT 0,
                next_scrape_after TEXT
            );

            CREATE TABLE IF NOT EXISTS doubles_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tourney_id TEXT,
                tourney_name TEXT,
                surface TEXT,
                draw_size INTEGER,
                tourney_level TEXT,
                tourney_date TEXT,
                match_num INTEGER,
                winner1_id TEXT,
                winner2_id TEXT,
                winner_seed TEXT,
                winner_entry TEXT,
                loser1_id TEXT,
                loser2_id TEXT,
                loser_seed TEXT,
                loser_entry TEXT,
                score TEXT,
                best_of INTEGER,
                round TEXT,
                winner1_name TEXT, winner1_hand TEXT, winner1_ht REAL,
                winner1_ioc TEXT, winner1_age REAL,
                winner2_name TEXT, winner2_hand TEXT, winner2_ht REAL,
                winner2_ioc TEXT, winner2_age REAL,
                loser1_name TEXT, loser1_hand TEXT, loser1_ht REAL,
                loser1_ioc TEXT, loser1_age REAL,
                loser2_name TEXT, loser2_hand TEXT, loser2_ht REAL,
                loser2_ioc TEXT, loser2_age REAL,
                winner1_rank REAL, winner1_rank_points REAL,
                winner2_rank REAL, winner2_rank_points REAL,
                loser1_rank REAL, loser1_rank_points REAL,
                loser2_rank REAL, loser2_rank_points REAL,
                minutes REAL,
                w_ace REAL, w_df REAL, w_svpt REAL, w_1stIn REAL,
                w_1stWon REAL, w_2ndWon REAL, w_SvGms REAL,
                w_bpSaved REAL, w_bpFaced REAL,
                l_ace REAL, l_df REAL, l_svpt REAL, l_1stIn REAL,
                l_1stWon REAL, l_2ndWon REAL, l_SvGms REAL,
                l_bpSaved REAL, l_bpFaced REAL,
                tour TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_doubles_tourney ON doubles_matches(tourney_name);
            CREATE INDEX IF NOT EXISTS idx_doubles_date ON doubles_matches(tourney_date);
            CREATE INDEX IF NOT EXISTS idx_matches_tour ON matches(tour);
            CREATE INDEX IF NOT EXISTS idx_doubles_tour ON doubles_matches(tour);
            CREATE INDEX IF NOT EXISTS idx_rankings_tour ON rankings(tour);

            -- Extended stats tables (scraped from player-more.cgi)
            CREATE TABLE IF NOT EXISTS match_winners_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                player_name TEXT,
                opponent_name TEXT,
                tourney_name TEXT,
                tourney_date TEXT,
                round TEXT,
                surface TEXT,
                tourney_level TEXT,
                score TEXT,
                winners INTEGER,
                unforced_errors INTEGER,
                w_ue_ratio REAL,
                winner_pct REAL,
                ue_pct REAL,
                opp_winners INTEGER,
                opp_unforced_errors INTEGER,
                opp_w_ue_ratio REAL,
                opp_winner_pct REAL,
                opp_ue_pct REAL,
                net_points_won INTEGER,
                net_points_total INTEGER,
                opp_net_points_won INTEGER,
                opp_net_points_total INTEGER,
                tour TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_we_player ON match_winners_errors(player_name);
            CREATE INDEX IF NOT EXISTS idx_we_date ON match_winners_errors(tourney_date);

            CREATE TABLE IF NOT EXISTS match_serve_speed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                player_name TEXT,
                opponent_name TEXT,
                tourney_name TEXT,
                tourney_date TEXT,
                round TEXT,
                surface TEXT,
                tourney_level TEXT,
                first_serve_avg REAL,
                first_serve_stdev REAL,
                first_serve_median REAL,
                first_serve_max REAL,
                first_serve_min REAL,
                second_serve_avg REAL,
                second_serve_stdev REAL,
                second_serve_median REAL,
                second_serve_max REAL,
                second_serve_min REAL,
                speed_unit TEXT DEFAULT 'mph',
                tour TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_speed_player ON match_serve_speed(player_name);

            CREATE TABLE IF NOT EXISTS match_pbp_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                player_name TEXT,
                opponent_name TEXT,
                tourney_name TEXT,
                tourney_date TEXT,
                round TEXT,
                surface TEXT,
                tourney_level TEXT,
                aggressive_margin REAL,
                serve_plus1_ratio REAL,
                baseline_pct REAL,
                rally_length_avg REAL,
                ace_on_serve_plus1_pct REAL,
                hold_after_ace_pct REAL,
                return_pts_won_pct REAL,
                opp_aggressive_margin REAL,
                opp_serve_plus1_ratio REAL,
                opp_baseline_pct REAL,
                opp_rally_length_avg REAL,
                tour TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pbp_player ON match_pbp_stats(player_name);

            CREATE TABLE IF NOT EXISTS match_mcp_serve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                player_name TEXT,
                opponent_name TEXT,
                tourney_name TEXT,
                tourney_date TEXT,
                round TEXT,
                surface TEXT,
                tourney_level TEXT,
                deuce_wide_pct REAL,
                deuce_body_pct REAL,
                deuce_t_pct REAL,
                ad_wide_pct REAL,
                ad_body_pct REAL,
                ad_t_pct REAL,
                ace_pct REAL,
                unreturned_pct REAL,
                first_wide_pct REAL,
                first_body_pct REAL,
                first_t_pct REAL,
                second_wide_pct REAL,
                second_body_pct REAL,
                second_t_pct REAL,
                tour TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_serve_player ON match_mcp_serve(player_name);

            CREATE TABLE IF NOT EXISTS match_mcp_return (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                player_name TEXT,
                opponent_name TEXT,
                tourney_name TEXT,
                tourney_date TEXT,
                round TEXT,
                surface TEXT,
                tourney_level TEXT,
                return_in_play_pct REAL,
                return_depth_deep_pct REAL,
                return_fh_pct REAL,
                return_bh_pct REAL,
                first_return_in_play_pct REAL,
                second_return_in_play_pct REAL,
                tour TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_return_player ON match_mcp_return(player_name);

            CREATE TABLE IF NOT EXISTS match_mcp_rally (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                player_name TEXT,
                opponent_name TEXT,
                tourney_name TEXT,
                tourney_date TEXT,
                round TEXT,
                surface TEXT,
                tourney_level TEXT,
                rally_length_avg REAL,
                fh_pct REAL,
                bh_pct REAL,
                net_approach_pct REAL,
                rally_won_0_4 REAL,
                rally_won_5_8 REAL,
                rally_won_9_plus REAL,
                tour TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_rally_player ON match_mcp_rally(player_name);

            CREATE TABLE IF NOT EXISTS match_mcp_tactics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                player_name TEXT,
                opponent_name TEXT,
                tourney_name TEXT,
                tourney_date TEXT,
                round TEXT,
                surface TEXT,
                tourney_level TEXT,
                net_approach_pct REAL,
                net_points_won_pct REAL,
                dropshot_pct REAL,
                dropshot_won_pct REAL,
                serve_and_volley_pct REAL,
                sv_won_pct REAL,
                inside_in_fh_pct REAL,
                inside_out_fh_pct REAL,
                tour TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_tactics_player ON match_mcp_tactics(player_name);

            -- Cache for extended stats scraping
            CREATE TABLE IF NOT EXISTS extended_stats_cache (
                player_name TEXT PRIMARY KEY,
                last_scraped TEXT NOT NULL,
                tables_scraped TEXT,
                activity_fingerprint TEXT
            );
        """)
        # Migrate existing rankings table: add columns introduced with
        # the live-tennis.eu switch (safe to call repeatedly).
        for col, typ in [("age", "INTEGER"), ("rank_diff", "INTEGER"),
                         ("pts_diff", "INTEGER"), ("next_tournament", "TEXT"),
                         ("ioc", "TEXT")]:
            try:
                cur.execute(f"ALTER TABLE rankings ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # column already exists
        # Migrate existing players table: add tour column.
        try:
            cur.execute("ALTER TABLE players ADD COLUMN tour TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migrate scrape_cache: add last_match_date column.
        try:
            cur.execute("ALTER TABLE scrape_cache ADD COLUMN last_match_date TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migrate scrape_cache: add activity_fingerprint column.
        try:
            cur.execute("ALTER TABLE scrape_cache ADD COLUMN activity_fingerprint TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migrate scrape_cache: add match_signature column.
        try:
            cur.execute("ALTER TABLE scrape_cache ADD COLUMN match_signature TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migrate scrape_cache: add retry cooldown columns.
        for col, typ in [("scrape_retry_count", "INTEGER DEFAULT 0"),
                         ("next_scrape_after", "TEXT")]:
            try:
                cur.execute(f"ALTER TABLE scrape_cache ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # column already exists
        # Migrate extended_stats_cache: add activity_fingerprint column.
        try:
            cur.execute("ALTER TABLE extended_stats_cache ADD COLUMN activity_fingerprint TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migrate matches table: add is_upcoming flag for scheduled
        # matches that haven't been played yet (used by the matches UI
        # banner; analytics queries should filter is_upcoming=0).
        try:
            cur.execute(
                "ALTER TABLE matches ADD COLUMN is_upcoming INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_matches_upcoming "
                "ON matches(is_upcoming, winner_name, loser_name)")
        except sqlite3.OperationalError:
            pass
        # Migrate extended stats tables: add tourney_level column.
        for tbl in ("match_winners_errors", "match_serve_speed",
                     "match_pbp_stats", "match_mcp_serve",
                     "match_mcp_return", "match_mcp_rally",
                     "match_mcp_tactics"):
            try:
                cur.execute(f"ALTER TABLE {tbl} ADD COLUMN tourney_level TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists

        # One-shot migration: normalize player_name in extended-stats
        # tables and cache to the diacritic-free spelling used in the
        # ``players`` table (e.g. "Rafael Jódar" → "Rafael Jodar"), so
        # that lookups by ``f"{name_first} {name_last}"`` find the rows.
        try:
            self._migrate_normalize_extended_player_names(cur)
        except Exception:
            logger.exception(
                "Failed to normalize extended-stats player names "
                "(non-fatal, will retry on next start)")

        # One-shot migration: canonicalize winner_name / loser_name in
        # the ``matches`` table against the spellings used in the
        # ``players`` table (e.g. "Christopher Oconnell" →
        # "Christopher O'Connell", "Marvin Möller" → "Marvin Moller"),
        # then collapse duplicate match rows that differed only by name
        # spelling.
        try:
            self._migrate_normalize_match_player_names(cur)
        except Exception:
            logger.exception(
                "Failed to normalize matches.winner_name/loser_name "
                "(non-fatal, will retry on next start)")

        # One-shot migration: fix scraped WTA matches stored with tour='atp'.
        # Uses the players table (which is correctly split by tour) to detect
        # WTA-only names and updates the tour field accordingly.
        try:
            self._migrate_fix_scraped_match_tour(cur)
        except Exception:
            logger.exception(
                "Failed to fix scraped match tour field "
                "(non-fatal, will retry on next start)")
        # --- Phase 2 composite indexes for faster queries ---
        cur.executescript("""
            -- Matches: composite indexes for common query patterns
            CREATE INDEX IF NOT EXISTS idx_matches_tour_date
                ON matches(tour, tourney_date);
            CREATE INDEX IF NOT EXISTS idx_matches_winner_tour
                ON matches(winner_id, tour, tourney_date);
            CREATE INDEX IF NOT EXISTS idx_matches_loser_tour
                ON matches(loser_id, tour, tourney_date);
            CREATE INDEX IF NOT EXISTS idx_matches_tourney_date
                ON matches(tourney_name, tourney_date);

            -- Rankings: composite for ranking lookups
            CREATE INDEX IF NOT EXISTS idx_rankings_pid_date
                ON rankings(player_id, ranking_date);
            CREATE INDEX IF NOT EXISTS idx_rankings_tour_date
                ON rankings(tour, ranking_date);

            -- Extended stats: composite (player_name, tourney_date) for
            -- all get_player_* queries that ORDER BY tourney_date DESC
            CREATE INDEX IF NOT EXISTS idx_we_player_date
                ON match_winners_errors(player_name, tourney_date);
            CREATE INDEX IF NOT EXISTS idx_speed_player_date
                ON match_serve_speed(player_name, tourney_date);
            CREATE INDEX IF NOT EXISTS idx_pbp_player_date
                ON match_pbp_stats(player_name, tourney_date);
            CREATE INDEX IF NOT EXISTS idx_mcp_serve_player_date
                ON match_mcp_serve(player_name, tourney_date);
            CREATE INDEX IF NOT EXISTS idx_mcp_return_player_date
                ON match_mcp_return(player_name, tourney_date);
            CREATE INDEX IF NOT EXISTS idx_mcp_rally_player_date
                ON match_mcp_rally(player_name, tourney_date);
            CREATE INDEX IF NOT EXISTS idx_mcp_tactics_player_date
                ON match_mcp_tactics(player_name, tourney_date);

            -- IOC: composite indexes for Nations-page queries
            CREATE INDEX IF NOT EXISTS idx_matches_winner_ioc
                ON matches(tour, winner_ioc, tourney_date);
            CREATE INDEX IF NOT EXISTS idx_matches_loser_ioc
                ON matches(tour, loser_ioc, tourney_date);

            -- Expression indexes so REPLACE(name, '-', ' ') = ? is fast
            CREATE INDEX IF NOT EXISTS idx_matches_winner_name_norm
                ON matches(REPLACE(winner_name, '-', ' '));
            CREATE INDEX IF NOT EXISTS idx_matches_loser_name_norm
                ON matches(REPLACE(loser_name, '-', ' '));
        """)
        self.conn.commit()

    def _run_analyze_once(self):
        """Run ANALYZE once to update query planner statistics.
        Skips on subsequent launches by checking a user_version flag."""
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            logger.info("Running ANALYZE to update query planner statistics...")
            self.conn.execute("ANALYZE")
            self.conn.execute("PRAGMA user_version = 2")
            self.conn.commit()
        if version < 3:
            # Re-analyze after adding IOC and REPLACE() expression indexes
            logger.info("Running ANALYZE for new indexes (version 3)...")
            self.conn.execute("ANALYZE matches")
            self.conn.execute("PRAGMA user_version = 3")
            self.conn.commit()
        if version < 4:
            # Remove SCRAPED duplicate rows that exist alongside CSV rows
            # for the same match (year + tourney_name + round + winner + loser).
            # Caused by scraper using per-match dates vs CSV using tourney-start date.
            logger.info("Removing SCRAPED duplicate rows shadowing CSV data (version 4)...")
            try:
                self.conn.execute("""
                    DELETE FROM matches
                    WHERE tourney_id = 'SCRAPED'
                      AND rowid IN (
                        SELECT scraped.rowid
                        FROM matches scraped
                        JOIN matches csv
                          ON SUBSTR(csv.tourney_date, 1, 4) = SUBSTR(scraped.tourney_date, 1, 4)
                         AND LOWER(csv.tourney_name) = LOWER(scraped.tourney_name)
                         AND csv.round = scraped.round
                         AND csv.winner_name = scraped.winner_name
                         AND csv.loser_name  = scraped.loser_name
                         AND csv.tourney_id != 'SCRAPED'
                        WHERE scraped.tourney_id = 'SCRAPED'
                      )
                """)
                self.conn.execute("PRAGMA user_version = 4")
                self.conn.commit()
                logger.info("Duplicate cleanup complete.")
            except Exception as exc:
                logger.warning("Duplicate cleanup failed: %s", exc)

    def has_data(self, tour="atp"):
        """Return True if this tour already has matches imported."""
        row = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM matches WHERE tour = ? AND tourney_id != 'SCRAPED' LIMIT 1)",
            (tour,)).fetchone()
        return row[0] == 1

    def has_doubles_data(self, tour="atp"):
        """Return True if this tour already has doubles matches imported."""
        row = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM doubles_matches WHERE tour = ? LIMIT 1)",
            (tour,)).fetchone()
        return row[0] == 1

    def import_doubles_only(self, tour="atp", year_start=1968, year_end=None):
        """Import only doubles matches (used when singles already imported)."""
        doubles = load_doubles(tour, year_start, year_end)
        if not doubles.empty:
            self.conn.execute(
                "DELETE FROM doubles_matches WHERE tour = ?", (tour,))
            self.conn.commit()
            doubles["tour"] = tour
            _chunk = max(1, 999 // max(len(doubles.columns), 1))
            doubles.to_sql("doubles_matches", self.conn,
                           if_exists="append", index=False,
                           method="multi", chunksize=_chunk)
            self.conn.commit()
            logger.info("Imported %d doubles matches for %s", len(doubles), tour)

    def import_data(self, tour="atp", year_start=1968, year_end=None,
                    progress_callback=None):
        """Import CSV data into the SQLite database."""
        if progress_callback:
            progress_callback(0, 4, "Loading players...")

        players = load_players(tour)
        if not players.empty:
            self._clear_table("players", tour)
            players["tour"] = tour
            players.to_sql("players", self.conn, if_exists="append", index=False)
            logger.info("Imported %d players", len(players))

        if progress_callback:
            progress_callback(1, 4, "Loading matches...")

        matches = load_matches(tour, year_start, year_end, include_qual=True)
        if not matches.empty:
            self._clear_matches(tour)
            matches["tour"] = tour
            # Normalize player names: strip diacritics for consistency
            for col in ("winner_name", "loser_name"):
                if col in matches.columns:
                    matches[col] = matches[col].apply(_strip_diacritics)
            _chunk = max(1, 999 // max(len(matches.columns), 1))
            matches.to_sql("matches", self.conn, if_exists="append", index=False,
                           method="multi", chunksize=_chunk)
            logger.info("Imported %d matches", len(matches))
            # Remove any SCRAPED rows that now duplicate the freshly-imported CSV
            # rows (same year + tourney_name + round + winner + loser).  This keeps
            # the DB clean after every CSV refresh without a one-time migration.
            deleted = self.conn.execute("""
                DELETE FROM matches
                WHERE tourney_id = 'SCRAPED' AND tour = ?
                  AND rowid IN (
                    SELECT scraped.rowid
                    FROM matches scraped
                    JOIN matches csv
                      ON SUBSTR(csv.tourney_date, 1, 4) = SUBSTR(scraped.tourney_date, 1, 4)
                     AND LOWER(csv.tourney_name) = LOWER(scraped.tourney_name)
                     AND csv.round = scraped.round
                     AND csv.winner_name = scraped.winner_name
                     AND csv.loser_name  = scraped.loser_name
                     AND csv.tourney_id != 'SCRAPED' AND csv.tour = ?
                    WHERE scraped.tourney_id = 'SCRAPED' AND scraped.tour = ?
                  )
            """, (tour, tour, tour))
            n_deleted = self.conn.execute("SELECT changes()").fetchone()[0]
            if n_deleted:
                logger.info("Removed %d SCRAPED rows duplicating CSV data", n_deleted)

        if progress_callback:
            progress_callback(2, 4, "Loading doubles...")

        doubles = load_doubles(tour, year_start, year_end)
        if not doubles.empty:
            self.conn.execute(
                "DELETE FROM doubles_matches WHERE tour = ?", (tour,))
            self.conn.commit()
            doubles["tour"] = tour
            _chunk = max(1, 999 // max(len(doubles.columns), 1))
            doubles.to_sql("doubles_matches", self.conn,
                           if_exists="append", index=False,
                           method="multi", chunksize=_chunk)
            logger.info("Imported %d doubles matches", len(doubles))

        if progress_callback:
            progress_callback(3, 4, "Loading rankings...")

        rankings = load_rankings(tour)
        if not rankings.empty:
            self._clear_rankings(tour)
            rankings = rankings.rename(columns={"player": "player_id"})
            # WTA CSVs have an extra 'tours' column not in our schema
            rankings = rankings.drop(columns=["tours"], errors="ignore")
            rankings["tour"] = tour
            _chunk = max(1, 999 // max(len(rankings.columns), 1))
            rankings.to_sql("rankings", self.conn, if_exists="append", index=False,
                           method="multi", chunksize=_chunk)
            logger.info("Imported %d ranking entries", len(rankings))

        if progress_callback:
            progress_callback(4, 4, "Import complete!")

        self.conn.commit()

    def _clear_table(self, table, tour):
        if table == "players":
            self.conn.execute("DELETE FROM players WHERE tour = ?", (tour,))
        self.conn.commit()

    def _clear_matches(self, tour):
        self.conn.execute(
            "DELETE FROM matches WHERE tour = ? AND tourney_id != 'SCRAPED'",
            (tour,))
        self.conn.commit()

    def _clear_rankings(self, tour):
        self.conn.execute(
            "DELETE FROM rankings WHERE tour = ? AND ranking_date != 'LIVE'",
            (tour,))
        self.conn.commit()

    def search_players(self, query, limit=20):
        """Search players by name (partial match), best matches first."""
        cur = self.conn.execute("""
            SELECT player_id, name_first, name_last, hand, dob, ioc, height, tour,
                   CASE
                     WHEN LOWER(name_first || ' ' || name_last) = LOWER(?) THEN 0
                     WHEN LOWER(name_last) = LOWER(?) THEN 1
                     WHEN LOWER(name_first) = LOWER(?) THEN 2
                     ELSE 3
                   END AS rank_score
            FROM players
            WHERE name_first || ' ' || name_last LIKE ?
            ORDER BY rank_score, name_last, name_first
            LIMIT ?
        """, (query, query, query, f"%{query}%", limit))
        return [dict(r) for r in cur.fetchall()]

    def get_player(self, player_id, tour=None):
        """Get a single player by ID."""
        if tour:
            cur = self.conn.execute(
                "SELECT * FROM players WHERE player_id = ? AND tour = ?",
                (player_id, tour))
        else:
            cur = self.conn.execute(
                "SELECT * FROM players WHERE player_id = ?", (player_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_player_matches(self, player_id, surface=None, tourney_level=None,
                           year=None, opponent_id=None, round_=None,
                           tour=None):
        """Get all matches for a player with optional filters."""
        # Resolve full name for this player_id so we can also catch SCRAPED
        # matches where winner_id/loser_id was left empty by the scraper.
        name_row = self.conn.execute(
            "SELECT name_first, name_last FROM players WHERE player_id = ? LIMIT 1",
            (player_id,)
        ).fetchone()
        player_name = (
            f"{name_row[0]} {name_row[1]}".strip() if name_row else None
        )

        if player_name:
            # Normalize hyphens: match both "Auger-Aliassime" and "Auger Aliassime"
            player_name_nohyphen = player_name.replace("-", " ")
            id_cond = (
                "(winner_id = ? OR loser_id = ? "
                "OR (winner_id = '' AND REPLACE(winner_name, '-', ' ') = ?) "
                "OR (loser_id  = '' AND REPLACE(loser_name,  '-', ' ') = ?))"
            )
            id_params = [player_id, player_id, player_name_nohyphen, player_name_nohyphen]
        else:
            id_cond = "(winner_id = ? OR loser_id = ?)"
            id_params = [player_id, player_id]

        conditions = [id_cond]
        params = id_params

        if tour:
            conditions.append("tour = ?")
            params.append(tour)

        if surface:
            conditions.append("surface = ?")
            params.append(surface)
        _eq_or_in("tourney_level", tourney_level, conditions, params)
        if year:
            conditions.append("tourney_date BETWEEN ? AND ?")
            params.extend([f"{year}0000", f"{year}9999"])
        if opponent_id:
            conditions.append(
                "((winner_id = ? AND loser_id = ?) OR "
                "(winner_id = ? AND loser_id = ?))"
            )
            params.extend([player_id, opponent_id, opponent_id, player_id])
        _eq_or_in("round", round_, conditions, params)
        # Always exclude scheduled/upcoming matches from analytics views.
        conditions.append("(is_upcoming = 0 OR is_upcoming IS NULL)")

        where = " AND ".join(conditions)
        query = f"""
            SELECT * FROM matches
            WHERE {where}
            ORDER BY tourney_date DESC,
                CASE round
                    WHEN 'F' THEN 1
                    WHEN 'SF' THEN 2
                    WHEN 'QF' THEN 3
                    WHEN 'R16' THEN 4
                    WHEN 'R32' THEN 5
                    WHEN 'R64' THEN 6
                    WHEN 'R128' THEN 7
                    WHEN 'RR' THEN 8
                    WHEN 'Q3' THEN 9
                    WHEN 'Q2' THEN 10
                    WHEN 'Q1' THEN 11
                    ELSE 12
                END,
                match_num DESC
        """
        cur = self.conn.execute(query, params)
        return [dict(r) for r in cur.fetchall()]

    def get_player_upcoming_match(self, player_name):
        """Return the next scheduled match for *player_name*, or None.

        Looks for rows in ``matches`` with ``is_upcoming = 1`` where
        the player appears as either winner or loser placeholder.
        Returns the soonest one (earliest tourney_date).
        """
        if not player_name:
            return None
        try:
            row = self.conn.execute("""
                SELECT * FROM matches
                WHERE is_upcoming = 1
                  AND (REPLACE(winner_name, '-', ' ') = ?
                       OR REPLACE(loser_name, '-', ' ') = ?)
                ORDER BY tourney_date ASC, match_num ASC
                LIMIT 1
            """, (player_name.replace("-", " "),
                  player_name.replace("-", " "))).fetchone()
        except sqlite3.OperationalError:
            # is_upcoming column not yet present (pre-migration).
            return None
        if not row:
            return None
        d = dict(row)
        # The "opponent" is whichever side is not the queried player.
        # We stored player_name as winner_name when scraping; if hyphen
        # normalisation differs, fall back to matching by substring.
        target = player_name.lower().replace("-", " ")
        wn = (d.get("winner_name") or "").lower().replace("-", " ")
        if wn == target:
            d["opponent"] = d.get("loser_name")
            d["opponent_seed"] = d.get("loser_seed")
            d["opponent_entry"] = d.get("loser_entry")
            d["opponent_ioc"] = d.get("loser_ioc")
            d["opponent_rank"] = d.get("loser_rank")
        else:
            d["opponent"] = d.get("winner_name")
            d["opponent_seed"] = d.get("winner_seed")
            d["opponent_entry"] = d.get("winner_entry")
            d["opponent_ioc"] = d.get("winner_ioc")
            d["opponent_rank"] = d.get("winner_rank")
        return d

    def get_player_career_stats(self, player_id, tour=None):
        """Calculate career statistics for a player."""
        _cache_key = (player_id, tour)
        if _cache_key in self._career_stats_cache:
            return self._career_stats_cache[_cache_key]

        tour_cond = " AND tour = ?" if tour else ""
        tour_params = (tour,) if tour else ()

        # Resolve full name so we also catch SCRAPED matches where the
        # scraper left winner_id/loser_id empty (typical for recent TA
        # rows post-2024).  Mirrors the logic in ``get_player_matches``.
        name_row = self.conn.execute(
            "SELECT name_first, name_last FROM players "
            "WHERE player_id = ? LIMIT 1",
            (player_id,)
        ).fetchone()
        player_name = (
            f"{name_row[0]} {name_row[1]}".strip() if name_row else None
        )
        if player_name:
            player_name_nohyphen = player_name.replace("-", " ")
            win_match = (
                "(winner_id = ? OR (winner_id = '' "
                "AND REPLACE(winner_name, '-', ' ') = ?))"
            )
            lose_match = (
                "(loser_id = ? OR (loser_id = '' "
                "AND REPLACE(loser_name, '-', ' ') = ?))"
            )
            win_params = (player_id, player_name_nohyphen)
            lose_params = (player_id, player_name_nohyphen)
        else:
            win_match = "winner_id = ?"
            lose_match = "loser_id = ?"
            win_params = (player_id,)
            lose_params = (player_id,)

        # Single query to get all breakdowns at once
        rows = self.conn.execute(f"""
            SELECT side, surface, tourney_level, round,
                   SUBSTR(tourney_date, 1, 4) as yr, COUNT(*) as cnt,
                   SUM(aces) as aces, SUM(dfs) as dfs,
                   SUM(svpt) as svpt, SUM(first_in) as first_in,
                   SUM(first_won) as first_won, SUM(second_won) as second_won,
                   SUM(bp_saved) as bp_saved, SUM(bp_faced) as bp_faced
            FROM (
                SELECT 'W' as side, surface, tourney_level, round, tourney_date,
                       w_ace as aces, w_df as dfs, w_svpt as svpt,
                       w_1stIn as first_in, w_1stWon as first_won,
                       w_2ndWon as second_won, w_bpSaved as bp_saved,
                       w_bpFaced as bp_faced
                FROM matches WHERE {win_match}{tour_cond}
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND UPPER(COALESCE(score,'')) NOT LIKE '%W/O%'
                UNION ALL
                SELECT 'L' as side, surface, tourney_level, round, tourney_date,
                       l_ace as aces, l_df as dfs, l_svpt as svpt,
                       l_1stIn as first_in, l_1stWon as first_won,
                       l_2ndWon as second_won, l_bpSaved as bp_saved,
                       l_bpFaced as bp_faced
                FROM matches WHERE {lose_match}{tour_cond}
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND UPPER(COALESCE(score,'')) NOT LIKE '%W/O%'
            )
            GROUP BY side, surface, tourney_level, round, yr
        """, win_params + tour_params + lose_params + tour_params).fetchall()

        wins = 0
        losses = 0
        surfaces = {}
        levels = {}
        level_rounds = {}   # level_name -> {F_w, F_l, SF, QF, R16, R32, R64}
        rounds = {}
        titles = 0
        yearly = {}
        total_aces = 0
        total_dfs = 0
        total_svpt = 0
        total_first_in = 0
        total_first_won = 0
        total_second_won = 0
        total_bp_saved = 0
        total_bp_faced = 0

        level_names = {
            "G": "Grand Slam",
            "M": "Masters 1000",      "PM": "Premier Mandatory",
            "A": "ATP 250/500",       "P": "Premier",
            "D": "Davis Cup",         "I": "International",
            "F": "Tour Finals",       "E": "Elite Trophy",
        }

        for r in rows:
            side = r["side"]
            cnt = r["cnt"]
            surf = r["surface"] or "Unknown"
            lev = r["tourney_level"] or ""
            rnd = r["round"] or ""
            yr = r["yr"] or ""

            if side == "W":
                wins += cnt
                if rnd == "F":
                    titles += cnt
            else:
                losses += cnt

            # Surface
            surfaces.setdefault(surf, {"wins": 0, "losses": 0})
            surfaces[surf]["wins" if side == "W" else "losses"] += cnt

            # Level
            lev_name = level_names.get(lev)
            if lev_name:
                levels.setdefault(lev_name, {"wins": 0, "losses": 0})
                levels[lev_name]["wins" if side == "W" else "losses"] += cnt

                # Per-level round breakdown
                lr = level_rounds.setdefault(lev_name, {
                    "F_w": 0, "F_l": 0, "SF": 0, "QF": 0,
                    "R16": 0, "R32": 0, "R64": 0,
                })
                if rnd == "F":
                    if side == "W":
                        lr["F_w"] += cnt
                    else:
                        lr["F_l"] += cnt
                elif rnd in ("SF", "QF", "R16", "R32", "R64"):
                    lr[rnd] += cnt

            # Round
            if rnd in ("F", "SF", "QF", "R16", "R32", "R64", "R128", "RR"):
                rounds.setdefault(rnd, {"wins": 0, "losses": 0})
                rounds[rnd]["wins" if side == "W" else "losses"] += cnt

            # Yearly
            if yr:
                yearly.setdefault(yr, {"wins": 0, "losses": 0})
                yearly[yr]["wins" if side == "W" else "losses"] += cnt

            # Serve stats
            total_aces += int(r["aces"] or 0)
            total_dfs += int(r["dfs"] or 0)
            total_svpt += int(r["svpt"] or 0)
            total_first_in += int(r["first_in"] or 0)
            total_first_won += int(r["first_won"] or 0)
            total_second_won += int(r["second_won"] or 0)
            total_bp_saved += int(r["bp_saved"] or 0)
            total_bp_faced += int(r["bp_faced"] or 0)

        # Remove surfaces with 0 matches
        surfaces = {k: v for k, v in surfaces.items()
                    if v["wins"] + v["losses"] > 0
                    and k in ("Hard", "Clay", "Grass", "Carpet")}

        serve = {}
        if total_svpt > 0:
            serve = {
                "aces": total_aces,
                "double_faults": total_dfs,
                "first_serve_pct": round(
                    total_first_in / total_svpt * 100, 1) if total_svpt else 0,
                "first_serve_won_pct": round(
                    total_first_won / total_first_in * 100, 1
                ) if total_first_in else 0,
                "second_serve_won_pct": round(
                    total_second_won / (total_svpt - total_first_in) * 100, 1
                ) if (total_svpt - total_first_in) else 0,
                "bp_saved_pct": round(
                    total_bp_saved / (total_bp_faced or 1) * 100, 1),
            }

        result = {
            "wins": wins,
            "losses": losses,
            "titles": titles,
            "surfaces": surfaces,
            "levels": levels,
            "level_rounds": level_rounds,
            "rounds": rounds,
            "serve": serve,
            "yearly": yearly,
        }
        self._career_stats_cache[_cache_key] = result
        return result

    def get_player_streaks(self, player_id: str, tour: str | None = None) -> dict:
        """
        Compute consecutive-win / consecutive-sets streaks for a player,
        both overall and broken down by tourney_level.

        Returns:
            best_win_streak      : longest ever win streak (overall)
            current_win_streak   : active win streak (overall)
            best_sets_streak     : longest run of consecutive sets won (overall)
            current_sets_streak  : active sets streak (overall)
            by_level             : dict level_name -> {best_win, cur_win,
                                                       best_sets, cur_sets}
        """
        import re as _re

        level_names = {
            "G": "Grand Slam",
            "M": "Masters 1000",      "PM": "Premier Mandatory",
            "A": "ATP 250/500",       "P": "Premier",
            "D": "Davis Cup",         "I": "International",
            "F": "Tour Finals",       "E": "Elite Trophy",
        }

        tour_cond = " AND m.tour = ?" if tour else ""
        tour_params = (tour,) if tour else ()

        name_row = self.conn.execute(
            "SELECT name_first, name_last FROM players WHERE player_id = ? LIMIT 1",
            (player_id,),
        ).fetchone()
        pname = f"{name_row[0]} {name_row[1]}".strip() if name_row else None
        pname_nohyphen = pname.replace("-", " ") if pname else None

        if pname_nohyphen:
            win_match = (
                "(m.winner_id = ? OR (m.winner_id = '' "
                "AND REPLACE(m.winner_name, '-', ' ') = ?))"
            )
            lose_match = (
                "(m.loser_id = ? OR (m.loser_id = '' "
                "AND REPLACE(m.loser_name, '-', ' ') = ?))"
            )
            win_p = (player_id, pname_nohyphen)
            lose_p = (player_id, pname_nohyphen)
        else:
            win_match = "m.winner_id = ?"
            lose_match = "m.loser_id = ?"
            win_p = (player_id,)
            lose_p = (player_id,)

        sql = f"""
            SELECT side, score, tourney_level FROM (
                SELECT 'W' AS side, score, tourney_date, match_num, round,
                       tourney_level
                FROM matches m
                WHERE {win_match}{tour_cond}
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND UPPER(COALESCE(score,'')) NOT LIKE '%W/O%'
                UNION ALL
                SELECT 'L' AS side, score, tourney_date, match_num, round,
                       tourney_level
                FROM matches m
                WHERE {lose_match}{tour_cond}
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND UPPER(COALESCE(score,'')) NOT LIKE '%W/O%'
            )
            ORDER BY tourney_date ASC,
                CASE round
                    WHEN 'Q1' THEN 1
                    WHEN 'Q2' THEN 2
                    WHEN 'Q3' THEN 3
                    WHEN 'R128' THEN 4
                    WHEN 'R64' THEN 5
                    WHEN 'R32' THEN 6
                    WHEN 'R16' THEN 7
                    WHEN 'QF' THEN 8
                    WHEN 'SF' THEN 9
                    WHEN 'F' THEN 10
                    ELSE 11
                END,
                match_num DESC
        """
        rows = self.conn.execute(
            sql, win_p + tour_params + lose_p + tour_params
        ).fetchall()


        # overall accumulators
        best_win = 0
        cur_win = 0
        best_sets = 0
        cur_sets = 0

        # per-level accumulators  {level_name: [best_win, cur_win, best_sets, cur_sets]}
        lv: dict[str, list[int]] = {}

        def _parse_sets(score: str, won: bool):
            """Yield True/False per set indicating whether player won it."""
            clean = _re.sub(r'\s*(RET|Ret|ret)\.?$', '', score).strip()
            for token in clean.split():
                m_tok = _re.match(r'\[?(\d+)-(\d+)\]?(?:\(\d+\))?$', token)
                if not m_tok:
                    continue
                w_g, l_g = int(m_tok.group(1)), int(m_tok.group(2))
                if w_g == l_g:
                    continue
                yield (w_g > l_g) if won else (l_g > w_g)

        for r in rows:
            side = r["side"]
            score = r["score"] or ""
            lev_name = level_names.get(r["tourney_level"] or "")
            won = (side == "W")

            # ---- overall win streak (ALL match types in chronological order) ----
            if won:
                cur_win += 1
                if cur_win > best_win:
                    best_win = cur_win
            else:
                cur_win = 0

            # ---- overall sets streak ----
            for player_won_set in _parse_sets(score, won):
                if player_won_set:
                    cur_sets += 1
                    if cur_sets > best_sets:
                        best_sets = cur_sets
                else:
                    cur_sets = 0

            # ---- per-level (only recognized levels) ----
            if not lev_name:
                continue

            if lev_name not in lv:
                lv[lev_name] = [0, 0, 0, 0]  # best_win, cur_win, best_sets, cur_sets
            lv_acc = lv[lev_name]
            if won:
                lv_acc[1] += 1
                if lv_acc[1] > lv_acc[0]:
                    lv_acc[0] = lv_acc[1]
            else:
                lv_acc[1] = 0

            for player_won_set in _parse_sets(score, won):
                if player_won_set:
                    lv_acc[3] += 1
                    if lv_acc[3] > lv_acc[2]:
                        lv_acc[2] = lv_acc[3]
                else:
                    lv_acc[3] = 0

        by_level = {
            name: {
                "best_win": acc[0], "cur_win": acc[1],
                "best_sets": acc[2], "cur_sets": acc[3],
            }
            for name, acc in lv.items()
        }

        return {
            "best_win_streak": best_win,
            "current_win_streak": cur_win,
            "best_sets_streak": best_sets,
            "current_sets_streak": cur_sets,
            "by_level": by_level,
        }

    def get_head_to_head(self, player1_id, player2_id, tour=None):
        """Get head-to-head record and matches between two players."""
        tour_cond = " AND tour = ?" if tour else ""
        tour_params = (tour,) if tour else ()
        matches = self.conn.execute(f"""
            SELECT * FROM matches
            WHERE ((winner_id = ? AND loser_id = ?)
               OR (winner_id = ? AND loser_id = ?)){tour_cond}
              AND (is_upcoming = 0 OR is_upcoming IS NULL)
            ORDER BY tourney_date DESC
        """, (player1_id, player2_id, player2_id, player1_id) + tour_params).fetchall()

        p1_wins = sum(1 for m in matches if m["winner_id"] == player1_id)
        p2_wins = sum(1 for m in matches if m["winner_id"] == player2_id)

        # By surface
        h2h_surfaces = {}
        for m in matches:
            s = m["surface"] or "Unknown"
            h2h_surfaces.setdefault(s, {"p1_wins": 0, "p2_wins": 0})
            if m["winner_id"] == player1_id:
                h2h_surfaces[s]["p1_wins"] += 1
            else:
                h2h_surfaces[s]["p2_wins"] += 1

        return {
            "p1_wins": p1_wins,
            "p2_wins": p2_wins,
            "total_matches": len(matches),
            "by_surface": h2h_surfaces,
            "matches": [dict(m) for m in matches],
        }

    def get_rankings(self, tour="atp", date=None, top_n=100):
        """Get rankings, optionally filtered by date."""
        if date:
            target_date = date
        else:
            # Get latest ranking date
            latest = self.conn.execute("""
                SELECT MAX(ranking_date) FROM rankings WHERE tour = ?
            """, (tour,)).fetchone()[0]
            if not latest:
                return [], None
            target_date = latest

        # Use LEFT JOIN so LIVE rankings (where player_id may be a name
        # string rather than a real ID) still appear
        cur = self.conn.execute("""
            SELECT r.ranking_date, r.rank, r.player_id, r.points,
                   r.tour, r.age, r.rank_diff, r.pts_diff, r.next_tournament,
                   p.name_first, p.name_last,
                   COALESCE(p.ioc, r.ioc) AS ioc
            FROM rankings r
            LEFT JOIN players p ON r.player_id = p.player_id
                                   AND r.tour = p.tour
            WHERE r.tour = ? AND r.ranking_date = ?
            ORDER BY r.rank
            LIMIT ?
        """, (tour, target_date, top_n))

        results = []
        for row in cur:
            d = dict(row)
            # For LIVE rankings where player_id is actually a name string
            if d.get("name_first") is None and d.get("name_last") is None:
                pid = d.get("player_id", "")
                # player_id holds the full name when unresolved
                parts = pid.split(None, 1)
                if len(parts) == 2:
                    d["name_first"] = parts[0]
                    d["name_last"] = parts[1]
                else:
                    d["name_first"] = ""
                    d["name_last"] = pid
            results.append(d)

        return results, target_date

    def get_player_ranking_history(self, player_id, tour):
        """Return the full official ranking history for a player.

        Result is a list of dicts ordered by date ascending, each:
            {"ranking_date": "YYYYMMDD", "rank": int, "points": int|None}

        Only rows with a non-NULL rank are included (LIVE/preview rows
        without a real rank are skipped).  Index `idx_rankings_pid_date`
        keeps this fast.
        """
        if not player_id:
            return []
        _cache_key = (player_id, tour)
        if _cache_key in self._ranking_history_cache:
            return self._ranking_history_cache[_cache_key]
        cur = self.conn.execute("""
            SELECT ranking_date, rank, points
            FROM rankings
            WHERE player_id = ? AND tour = ? AND rank IS NOT NULL
              AND ranking_date GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
            ORDER BY ranking_date
        """, (str(player_id), tour))
        rows = [dict(r) for r in cur.fetchall()]
        self._ranking_history_cache[_cache_key] = rows
        return rows

    def invalidate_player_cache(self, player_id=None):
        """Clear in-memory result caches.

        Call with no arguments to wipe everything (e.g. after a data
        refresh), or with a player_id to evict only that player's entries.
        """
        if player_id is None:
            self._career_stats_cache.clear()
            self._ranking_history_cache.clear()
        else:
            keys = [k for k in self._career_stats_cache if k[0] == player_id]
            for k in keys:
                del self._career_stats_cache[k]
            keys = [k for k in self._ranking_history_cache if k[0] == player_id]
            for k in keys:
                del self._ranking_history_cache[k]

    # ------------------------------------------------------------------
    # Rank-filling helpers
    # ------------------------------------------------------------------

    def _fill_missing_ranks(self, matches):
        """Fill missing ranks using cross-reference + bounded fallbacks."""
        if not matches:
            return

        # --- pass 1: collect known ranks per player name from this result set ---
        name_rank = {}
        for m in matches:
            wn = (m.get("winner_name") or "").lower()
            ln = (m.get("loser_name") or "").lower()
            wr = m.get("winner_rank")
            lr = m.get("loser_rank")
            if wr and wn and wn not in name_rank:
                name_rank[wn] = wr
            if lr and ln and ln not in name_rank:
                name_rank[ln] = lr

        # --- pass 2: fill directly from cross-reference ---
        still_missing = set()
        for m in matches:
            wn = (m.get("winner_name") or "").lower()
            ln = (m.get("loser_name") or "").lower()
            if not m.get("winner_rank") and wn:
                if wn in name_rank:
                    m["winner_rank"] = name_rank[wn]
                else:
                    still_missing.add((wn, m.get("winner_id"), m.get("winner_name")))
            if not m.get("loser_rank") and ln:
                if ln in name_rank:
                    m["loser_rank"] = name_rank[ln]
                else:
                    still_missing.add((ln, m.get("loser_id"), m.get("loser_name")))

        if not still_missing:
            return

        # Determine tournament date and tour for bounded fallbacks
        tourney_date = None
        tour = None
        for m in matches:
            td = m.get("tourney_date")
            if td:
                tourney_date = str(td)
            if m.get("tour"):
                tour = m.get("tour")
            if tourney_date and tour:
                break

        from datetime import datetime, timedelta
        td_date = None
        min_dt_90 = None
        max_dt_90 = None
        min_dt_120 = None
        allow_scraped_fallback = False
        if tourney_date:
            try:
                td_date = datetime.strptime(tourney_date[:8], "%Y%m%d")
                min_dt_90 = (td_date - timedelta(days=90)).strftime("%Y%m%d")
                max_dt_90 = (td_date + timedelta(days=90)).strftime("%Y%m%d")
                min_dt_120 = (td_date - timedelta(days=120)).strftime("%Y%m%d")
                allow_scraped_fallback = abs((datetime.now() - td_date).days) <= 30
            except ValueError:
                td_date = None

        # --- pass 3: nearby matches within ±90 days ---
        nearby_rank = {}
        remaining = list(still_missing)
        if td_date and min_dt_90 and max_dt_90 and remaining:
            missing_names = [pname for _, _, pname in remaining if pname]
            if missing_names:
                placeholders = ",".join("?" * len(missing_names))
                tour_clause_w = " AND tour = ?" if tour else ""
                tour_clause_l = " AND tour = ?" if tour else ""
                params = missing_names + [min_dt_90, max_dt_90]
                if tour:
                    params.append(tour)
                params += missing_names + [min_dt_90, max_dt_90]
                if tour:
                    params.append(tour)

                cur = self.conn.execute(f"""
                    SELECT name, rank_val, tourney_date FROM (
                        SELECT winner_name AS name,
                               winner_rank AS rank_val, tourney_date
                        FROM matches
                        WHERE winner_name IN ({placeholders})
                          AND winner_rank IS NOT NULL
                          AND tourney_date BETWEEN ? AND ?
                          {tour_clause_w}
                        UNION ALL
                        SELECT loser_name AS name,
                               loser_rank AS rank_val, tourney_date
                        FROM matches
                        WHERE loser_name IN ({placeholders})
                          AND loser_rank IS NOT NULL
                          AND tourney_date BETWEEN ? AND ?
                          {tour_clause_l}
                    )
                """, params)

                best = {}  # name_lower -> (rank, day_diff)
                for row in cur:
                    name_low = (row[0] or "").lower()
                    rank_val = row[1]
                    rd = str(row[2] or "")
                    try:
                        rd_date = datetime.strptime(rd[:8], "%Y%m%d")
                        diff = abs((rd_date - td_date).days)
                    except (ValueError, TypeError):
                        diff = 999999999
                    prev = best.get(name_low)
                    if prev is None or diff < prev[1]:
                        best[name_low] = (rank_val, diff)
                for name_low, (rank_val, _) in best.items():
                    nearby_rank[name_low] = rank_val

        # --- pass 4: historical rankings within previous 120 days (same tour) ---
        hist_rank = {}
        remaining_after_nearby = [
            (n, pid, pn) for n, pid, pn in remaining if n not in nearby_rank
        ]
        if td_date and min_dt_120 and tour and remaining_after_nearby:
            ids = list({str(pid) for _, pid, _ in remaining_after_nearby if pid})
            if ids:
                placeholders = ",".join("?" * len(ids))
                cur = self.conn.execute(f"""
                    SELECT player_id, rank
                    FROM rankings
                    WHERE player_id IN ({placeholders})
                      AND tour = ?
                      AND ranking_date BETWEEN ? AND ?
                      AND ranking_date NOT LIKE 'SCRAPED_%'
                      AND ranking_date != 'LIVE'
                    ORDER BY ranking_date DESC
                """, ids + [tour, min_dt_120, tourney_date])
                for row in cur:
                    pid = str(row[0])
                    if pid not in hist_rank:
                        hist_rank[pid] = row[1]

        # --- pass 5: scraped current rankings (last resort for recent events) ---
        scraped_rank = {}
        remaining2 = [
            (n, pid, pn) for n, pid, pn in remaining_after_nearby
            if str(pid or "") not in hist_rank
        ]
        if allow_scraped_fallback and remaining2 and tour:
            cur = self.conn.execute("""
                SELECT r.rank, p.name_first, p.name_last
                FROM rankings r
                JOIN players p ON r.player_id = p.player_id
                              AND r.tour = p.tour
                WHERE r.ranking_date LIKE 'SCRAPED_%SINGLES'
                  AND r.tour = ?
                ORDER BY r.ranking_date DESC
            """, (tour,))
            for row in cur:
                first = row[1] or ""
                last = row[2] or ""
                full = f"{first} {last}".strip().lower()
                if full and full not in scraped_rank:
                    scraped_rank[full] = row[0]

        # --- apply fallbacks ---
        for m in matches:
            for side in ("winner", "loser"):
                if not m.get(f"{side}_rank"):
                    name = (m.get(f"{side}_name") or "").lower()
                    pid = str(m.get(f"{side}_id") or "")
                    rank = (nearby_rank.get(name)
                            or hist_rank.get(pid)
                            or scraped_rank.get(name))
                    if rank:
                        m[f"{side}_rank"] = rank

    def get_tournament_results(self, tourney_name=None, year=None, tour=None):
        """Get tournament draw/results, filling missing ranks."""
        conditions = []
        params = []
        if tourney_name:
            conditions.append("tourney_name LIKE ?")
            params.append(f"%{tourney_name}%")
        if year:
            conditions.append("tourney_date BETWEEN ? AND ?")
            params.extend([f"{year}0000", f"{year}9999"])
        if tour:
            conditions.append("tour = ?")
            params.append(tour)

        where = " AND ".join(conditions) if conditions else "1=1"
        cur = self.conn.execute(f"""
            SELECT * FROM matches
            WHERE {where}
              AND (is_upcoming = 0 OR is_upcoming IS NULL)
            ORDER BY tourney_date ASC,
                CASE round
                    WHEN 'Q1' THEN 1
                    WHEN 'Q2' THEN 2
                    WHEN 'Q3' THEN 3
                    WHEN 'R128' THEN 4
                    WHEN 'R64' THEN 5
                    WHEN 'R32' THEN 6
                    WHEN 'R16' THEN 7
                    WHEN 'QF' THEN 8
                    WHEN 'SF' THEN 9
                    WHEN 'F' THEN 10
                    ELSE 11
                END,
                match_num DESC
        """, params)
        results = [dict(r) for r in cur.fetchall()]
        self._fill_missing_ranks(results)
        return results

    def get_doubles_tournament_results(self, tourney_name=None, year=None,
                                       tour=None):
        """Get doubles tournament draw/results, filling missing ranks."""
        conditions = []
        params = []
        if tourney_name:
            conditions.append("tourney_name LIKE ?")
            params.append(f"%{tourney_name}%")
        if year:
            conditions.append("tourney_date BETWEEN ? AND ?")
            params.extend([f"{year}0000", f"{year}9999"])
        if tour:
            conditions.append("tour = ?")
            params.append(tour)

        where = " AND ".join(conditions) if conditions else "1=1"
        cur = self.conn.execute(f"""
            SELECT * FROM doubles_matches
            WHERE {where}
            ORDER BY tourney_date ASC,
                CASE round
                    WHEN 'Q1' THEN 1
                    WHEN 'Q2' THEN 2
                    WHEN 'Q3' THEN 3
                    WHEN 'R128' THEN 4
                    WHEN 'R64' THEN 5
                    WHEN 'R32' THEN 6
                    WHEN 'R16' THEN 7
                    WHEN 'QF' THEN 8
                    WHEN 'SF' THEN 9
                    WHEN 'F' THEN 10
                    ELSE 11
                END,
                match_num DESC
        """, params)
        return [dict(r) for r in cur.fetchall()]

    def get_tournament_list(self, year=None, tour=None):
        """Get list of unique tournaments."""
        conditions = []
        params = []
        if year:
            conditions.append("tourney_date BETWEEN ? AND ?")
            params.extend([f"{year}0000", f"{year}9999"])
        if tour:
            conditions.append("tour = ?")
            params.append(tour)
        where = " AND ".join(conditions) if conditions else "1=1"

        cur = self.conn.execute(f"""
            SELECT DISTINCT tourney_name, tourney_id, surface, tourney_level,
                   tourney_date
            FROM matches
            WHERE {where}
              AND (is_upcoming = 0 OR is_upcoming IS NULL)
            ORDER BY tourney_date
        """, params)
        return [dict(r) for r in cur.fetchall()]

    def get_doubles_tournament_list(self, year=None, tour=None):
        """Get list of unique doubles tournaments."""
        conditions = []
        params = []
        if year:
            conditions.append("tourney_date BETWEEN ? AND ?")
            params.extend([f"{year}0000", f"{year}9999"])
        if tour:
            conditions.append("tour = ?")
            params.append(tour)
        where = " AND ".join(conditions) if conditions else "1=1"

        cur = self.conn.execute(f"""
            SELECT DISTINCT tourney_name, tourney_id, surface, tourney_level,
                   tourney_date
            FROM doubles_matches
            WHERE {where}
            ORDER BY tourney_date
        """, params)
        return [dict(r) for r in cur.fetchall()]

    def get_available_years(self, tour=None):
        """Get list of years with data."""
        if tour:
            cur = self.conn.execute("""
                SELECT DISTINCT SUBSTR(tourney_date, 1, 4) as year
                FROM matches WHERE tour = ?
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                ORDER BY year
            """, (tour,))
        else:
            cur = self.conn.execute("""
                SELECT DISTINCT SUBSTR(tourney_date, 1, 4) as year
                FROM matches
                WHERE (is_upcoming = 0 OR is_upcoming IS NULL)
                ORDER BY year
            """)
        return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Nationality / Insights queries
    # ------------------------------------------------------------------

    def get_distinct_ioc_codes(self, tour: str = "atp") -> list[str]:
        """Return sorted list of distinct IOC country codes present in matches."""
        cur = self.conn.execute("""
            SELECT DISTINCT ioc FROM (
                SELECT winner_ioc AS ioc FROM matches
                WHERE tour = ? AND winner_ioc IS NOT NULL AND winner_ioc != ''
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                UNION
                SELECT loser_ioc AS ioc FROM matches
                WHERE tour = ? AND loser_ioc IS NOT NULL AND loser_ioc != ''
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
            )
            ORDER BY ioc
        """, (tour, tour))
        return [r[0] for r in cur.fetchall()]

    def get_nationality_round_breakdown(
        self,
        ioc: str,
        tour: str = "atp",
        year_from: int = 1968,
        year_to: int = 2099,
        tourney_levels: list | None = None,
        historic_olympics: bool = False,
    ) -> list[dict]:
        """
        For each (tourney_level, round) return appearances and unique_players.
        Uses IOC column when available; falls back to players table lookup by
        name for recently-scraped matches that have empty winner_ioc/loser_ioc.
        """
        level_clause = ""
        level_clause_w = ""
        level_params: list = []
        if tourney_levels:
            if historic_olympics and "A" in tourney_levels:
                non_a = [c for c in tourney_levels if c != "A"]
                oly_cond = "(tourney_level = 'A' AND tourney_name LIKE '%lympic%')"
                oly_cond_w = "(w.tourney_level = 'A' AND w.tourney_name LIKE '%lympic%')"
                if non_a:
                    ph = ",".join("?" * len(non_a))
                    level_clause = f"AND (tourney_level IN ({ph}) OR {oly_cond})"
                    level_clause_w = f"AND (w.tourney_level IN ({ph}) OR {oly_cond_w})"
                    level_params = non_a
                else:
                    level_clause = f"AND {oly_cond}"
                    level_clause_w = f"AND {oly_cond_w}"
                    level_params = []
            else:
                placeholders = ",".join("?" * len(tourney_levels))
                level_clause = f"AND tourney_level IN ({placeholders})"
                level_clause_w = f"AND w.tourney_level IN ({placeholders})"
                level_params = list(tourney_levels)

        # ioc_match_w / ioc_match_l: true when the player's nationality is ioc,
        # either via the stored IOC column or via a name lookup in the players table.
        ioc_winner = (
            "(winner_ioc = ?"
            " OR (COALESCE(winner_ioc,'')='' AND winner_name IN ("
            "     SELECT name_first||' '||name_last FROM players"
            "     WHERE ioc=? AND tour=?)))"
        )
        ioc_loser = (
            "(loser_ioc = ?"
            " OR (COALESCE(loser_ioc,'')='' AND loser_name IN ("
            "     SELECT name_first||' '||name_last FROM players"
            "     WHERE ioc=? AND tour=?)))"
        )

        sql = f"""
            SELECT tourney_level, round,
                   SUM(CASE WHEN side='W' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN side='L' THEN 1 ELSE 0 END) AS losses,
                   COUNT(*) AS appearances,
                   COUNT(DISTINCT COALESCE(NULLIF(player_id,''), player_name))
                     AS unique_players
            FROM (
                -- Played matches (winner side)
                SELECT tourney_level, round,
                       winner_id AS player_id, winner_name AS player_name,
                       'W' AS side
                FROM matches
                WHERE tour = ?
                  AND tourney_date BETWEEN CAST(? AS TEXT)||'0101'
                                      AND CAST(? AS TEXT)||'1231'
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND {ioc_winner}
                  {level_clause}
                UNION ALL
                -- Played matches (loser side)
                SELECT tourney_level, round,
                       loser_id AS player_id, loser_name AS player_name,
                       'L' AS side
                FROM matches
                WHERE tour = ?
                  AND tourney_date BETWEEN CAST(? AS TEXT)||'0101'
                                      AND CAST(? AS TEXT)||'1231'
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND {ioc_loser}
                  {level_clause}
                UNION ALL
                -- Upcoming appearances: winners of round X are guaranteed
                -- to appear in round X+1.  Only include if that next-round
                -- match does NOT already exist in the DB (either played or
                -- scheduled) to avoid double-counting.
                SELECT w.tourney_level,
                       CASE w.round
                         WHEN 'R128' THEN 'R64'  WHEN 'R64' THEN 'R32'
                         WHEN 'R32'  THEN 'R16'  WHEN 'R16' THEN 'QF'
                         WHEN 'QF'   THEN 'SF'   WHEN 'SF'  THEN 'F'
                       END AS round,
                       w.winner_id AS player_id,
                       w.winner_name AS player_name,
                       'P' AS side
                FROM matches w
                WHERE w.tour = ?
                  AND w.tourney_date BETWEEN CAST(? AS TEXT)||'0101'
                                        AND CAST(? AS TEXT)||'1231'
                  AND w.round IN ('R128','R64','R32','R16','QF','SF')
                  AND (w.is_upcoming = 0 OR w.is_upcoming IS NULL)
                  AND (
                      w.winner_ioc = ?
                      OR (COALESCE(w.winner_ioc,'')='' AND w.winner_name IN (
                          SELECT name_first||' '||name_last FROM players
                          WHERE ioc=? AND tour=?))
                  )
                  {level_clause_w}
                  -- Exclude if a match already exists at the projected round
                  AND NOT EXISTS (
                      SELECT 1 FROM matches nx
                      WHERE nx.tour = w.tour
                        AND nx.tourney_name = w.tourney_name
                        AND nx.tourney_date = w.tourney_date
                        AND (nx.winner_id = w.winner_id
                             OR nx.winner_name = w.winner_name
                             OR nx.loser_id   = w.winner_id
                             OR nx.loser_name  = w.winner_name)
                        AND nx.round = CASE w.round
                              WHEN 'R128' THEN 'R64'  WHEN 'R64' THEN 'R32'
                              WHEN 'R32'  THEN 'R16'  WHEN 'R16' THEN 'QF'
                              WHEN 'QF'   THEN 'SF'   WHEN 'SF'  THEN 'F'
                            END
                  )
            )
            GROUP BY tourney_level, round
            ORDER BY tourney_level, round
        """
        params = (
            [tour, year_from, year_to, ioc, ioc, tour] + level_params
            + [tour, year_from, year_to, ioc, ioc, tour] + level_params
            + [tour, year_from, year_to, ioc, ioc, tour] + level_params
        )
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_ioc_concentration_instances(
        self,
        ioc: str,
        min_count: int = 2,
        tour: str = "atp",
        year_from: int = 1968,
        year_to: int = 2099,
        tourney_levels: list | None = None,
        rounds: list | None = None,
        historic_olympics: bool = False,
    ) -> list[dict]:
        """
        Return tournament-year-round instances where >= min_count distinct players
        of the given IOC were present (either as winner or loser).
        Uses name-based IOC fallback for scraped matches with missing IOC column.
        """
        level_clause = ""
        level_clause_m = ""
        level_params: list = []
        if tourney_levels:
            if historic_olympics and "A" in tourney_levels:
                non_a = [c for c in tourney_levels if c != "A"]
                oly_cond = "(tourney_level = 'A' AND tourney_name LIKE '%lympic%')"
                oly_cond_m = "(m.tourney_level = 'A' AND m.tourney_name LIKE '%lympic%')"
                if non_a:
                    ph = ",".join("?" * len(non_a))
                    level_clause = f"AND (tourney_level IN ({ph}) OR {oly_cond})"
                    level_clause_m = f"AND (m.tourney_level IN ({ph}) OR {oly_cond_m})"
                    level_params = non_a
                else:
                    level_clause = f"AND {oly_cond}"
                    level_clause_m = f"AND {oly_cond_m}"
                    level_params = []
            else:
                placeholders = ",".join("?" * len(tourney_levels))
                level_clause = f"AND tourney_level IN ({placeholders})"
                level_clause_m = f"AND m.tourney_level IN ({placeholders})"
                level_params = list(tourney_levels)

        round_clause = ""
        projected_round_clause = ""
        round_params: list = []
        projected_round_params: list = []
        if rounds:
            placeholders = ",".join("?" * len(rounds))
            round_clause = f"AND round IN ({placeholders})"
            round_params = list(rounds)
            projected_round_clause = f"""
                  AND (CASE m.round
                         WHEN 'R128' THEN 'R64'  WHEN 'R64' THEN 'R32'
                         WHEN 'R32'  THEN 'R16'  WHEN 'R16' THEN 'QF'
                         WHEN 'QF'   THEN 'SF'   WHEN 'SF'  THEN 'F'
                       END) IN ({placeholders})
            """
            projected_round_params = list(rounds)

        ioc_winner = (
            "(winner_ioc = ?"
            " OR (COALESCE(winner_ioc,'')='' AND winner_name IN ("
            "     SELECT name_first||' '||name_last FROM players"
            "     WHERE ioc=? AND tour=?)))"
        )
        ioc_loser = (
            "(loser_ioc = ?"
            " OR (COALESCE(loser_ioc,'')='' AND loser_name IN ("
            "     SELECT name_first||' '||name_last FROM players"
            "     WHERE ioc=? AND tour=?)))"
        )
        ioc_winner_m = (
            "(m.winner_ioc = ?"
            " OR (COALESCE(m.winner_ioc,'')='' AND m.winner_name IN ("
            "     SELECT name_first||' '||name_last FROM players"
            "     WHERE ioc=? AND tour=?)))"
        )

        sql = f"""
            SELECT tourney_name, tourney_level, surface,
                   CAST(SUBSTR(tourney_date, 1, 4) AS INTEGER) AS year,
                   round,
                   COUNT(DISTINCT COALESCE(NULLIF(player_id,''), player_name))
                     AS ioc_count
            FROM (
                SELECT tourney_name, tourney_level, surface, tourney_date,
                       round, winner_id AS player_id, winner_name AS player_name
                FROM matches
                WHERE tour = ?
                  AND tourney_date BETWEEN CAST(? AS TEXT)||'0101'
                                      AND CAST(? AS TEXT)||'1231'
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND {ioc_winner}
                  {level_clause} {round_clause}
                UNION
                SELECT tourney_name, tourney_level, surface, tourney_date,
                       round, loser_id AS player_id, loser_name AS player_name
                FROM matches
                WHERE tour = ?
                  AND tourney_date BETWEEN CAST(? AS TEXT)||'0101'
                                      AND CAST(? AS TEXT)||'1231'
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND {ioc_loser}
                  {level_clause} {round_clause}
                UNION
                SELECT m.tourney_name, m.tourney_level, m.surface, m.tourney_date,
                       CASE m.round
                         WHEN 'R128' THEN 'R64'  WHEN 'R64' THEN 'R32'
                         WHEN 'R32'  THEN 'R16'  WHEN 'R16' THEN 'QF'
                         WHEN 'QF'   THEN 'SF'   WHEN 'SF'  THEN 'F'
                       END AS round,
                       m.winner_id AS player_id, m.winner_name AS player_name
                FROM matches m
                WHERE m.tour = ?
                  AND m.tourney_date BETWEEN CAST(? AS TEXT)||'0101'
                                        AND CAST(? AS TEXT)||'1231'
                  AND m.round IN ('R128','R64','R32','R16','QF','SF')
                  AND (m.is_upcoming = 0 OR m.is_upcoming IS NULL)
                  AND {ioc_winner_m}
                  {level_clause_m}
                  {projected_round_clause}
                  AND NOT EXISTS (
                      SELECT 1 FROM matches nx
                      WHERE nx.tour = m.tour
                        AND nx.tourney_name = m.tourney_name
                        AND nx.tourney_date = m.tourney_date
                        AND (nx.winner_id = m.winner_id
                             OR nx.winner_name = m.winner_name
                             OR nx.loser_id   = m.winner_id
                             OR nx.loser_name  = m.winner_name)
                        AND nx.round = CASE m.round
                              WHEN 'R128' THEN 'R64'  WHEN 'R64' THEN 'R32'
                              WHEN 'R32'  THEN 'R16'  WHEN 'R16' THEN 'QF'
                              WHEN 'QF'   THEN 'SF'   WHEN 'SF'  THEN 'F'
                            END
                  )
            )
            GROUP BY tourney_name,
                     CAST(SUBSTR(tourney_date, 1, 4) AS INTEGER),
                     round
            HAVING ioc_count >= ?
            ORDER BY year DESC, tourney_level,
                CASE round
                    WHEN 'Q1' THEN 1
                    WHEN 'Q2' THEN 2
                    WHEN 'Q3' THEN 3
                    WHEN 'R128' THEN 4
                    WHEN 'R64' THEN 5
                    WHEN 'R32' THEN 6
                    WHEN 'R16' THEN 7
                    WHEN 'QF' THEN 8
                    WHEN 'SF' THEN 9
                    WHEN 'F' THEN 10
                    ELSE 11
                END
        """
        params = (
            [tour, year_from, year_to, ioc, ioc, tour] + level_params + round_params
            + [tour, year_from, year_to, ioc, ioc, tour] + level_params + round_params
            + [tour, year_from, year_to, ioc, ioc, tour] + level_params + projected_round_params
            + [min_count]
        )
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_nationality_round_players(
        self,
        ioc: str,
        tourney_level: str,
        round_: str,
        tour: str = "atp",
        year_from: int = 1968,
        year_to: int = 2099,
        historic_olympics: bool = False,
    ) -> list[dict]:
        """
        For a given IOC + level + round, return each player with their
        appearances (total), wins, losses, and the list of tournaments.
        Includes projected upcoming appearances (side='P') for players who
        already won the previous round but have no match yet at this round.
        Ordered by appearances DESC.
        """
        # Build the tourney_level condition — for historical Olympics data
        # (tourney_level='A' with Olympic name) we need a name-based filter
        # combined with the modern 'O' code.
        if historic_olympics and tourney_level == "A":
            level_cond = "(tourney_level = 'O' OR (tourney_level = 'A' AND tourney_name LIKE '%lympic%'))"
            level_cond_m = "(m.tourney_level = 'O' OR (m.tourney_level = 'A' AND m.tourney_name LIKE '%lympic%'))"
            level_params: list = []
        else:
            level_cond = "tourney_level = ?"
            level_cond_m = "m.tourney_level = ?"
            level_params = [tourney_level]
        ioc_winner = (
            "(winner_ioc = ?"
            " OR (COALESCE(winner_ioc,'')='' AND winner_name IN ("
            "     SELECT name_first||' '||name_last FROM players"
            "     WHERE ioc=? AND tour=?)))"
        )
        ioc_loser = (
            "(loser_ioc = ?"
            " OR (COALESCE(loser_ioc,'')='' AND loser_name IN ("
            "     SELECT name_first||' '||name_last FROM players"
            "     WHERE ioc=? AND tour=?)))"
        )
        _prev_round_map = {
            'F': 'SF', 'SF': 'QF', 'QF': 'R16',
            'R16': 'R32', 'R32': 'R64', 'R64': 'R128',
        }
        prev_round = _prev_round_map.get(round_)
        projected_union = ""
        projected_params: list = []
        if prev_round:
            projected_union = f"""
                UNION ALL
                  SELECT m.winner_name AS player_name,
                      'P' AS side,
                      m.tourney_date AS tourney_date,
                      m.tour AS tourney_tour,
                       m.tourney_name || ' (' || SUBSTR(m.tourney_date,1,4) || ')' AS tourney_info
                FROM matches m
                WHERE m.tour = ? AND m.round = ? AND {level_cond_m}
                  AND CAST(SUBSTR(m.tourney_date,1,4) AS INTEGER) BETWEEN ? AND ?
                  AND (m.is_upcoming = 0 OR m.is_upcoming IS NULL)
                  AND (
                      m.winner_ioc = ?
                      OR (COALESCE(m.winner_ioc,'')='' AND m.winner_name IN (
                          SELECT name_first||' '||name_last FROM players
                          WHERE ioc=? AND tour=?))
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM matches nx
                      WHERE nx.tour = m.tour
                        AND nx.tourney_name = m.tourney_name
                        AND nx.tourney_date = m.tourney_date
                        AND (nx.winner_id = m.winner_id
                             OR nx.winner_name = m.winner_name
                             OR nx.loser_id   = m.winner_id
                             OR nx.loser_name  = m.winner_name)
                        AND nx.round = ?
                  )
            """
            projected_params = (
                [tour, prev_round] + level_params + [year_from, year_to,
                ioc, ioc, tour, round_]
            )
        sql = f"""
            SELECT player_name,
                   COUNT(*) AS appearances,
                   SUM(CASE WHEN side='W' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN side='L' THEN 1 ELSE 0 END) AS losses,
                     GROUP_CONCAT(side || '|' || tourney_date || '|' || tourney_tour || '|' || tourney_info) AS tournaments
            FROM (
                  SELECT * FROM (
                  SELECT winner_name AS player_name,
                      'W' AS side,
                      tourney_date,
                      tour AS tourney_tour,
                       tourney_name || ' (' || SUBSTR(tourney_date,1,4) || ')' AS tourney_info
                FROM matches
                WHERE tour = ? AND round = ? AND {level_cond}
                  AND CAST(SUBSTR(tourney_date,1,4) AS INTEGER) BETWEEN ? AND ?
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND {ioc_winner}
                UNION ALL
                  SELECT loser_name AS player_name,
                      'L' AS side,
                      tourney_date,
                      tour AS tourney_tour,
                       tourney_name || ' (' || SUBSTR(tourney_date,1,4) || ')' AS tourney_info
                FROM matches
                WHERE tour = ? AND round = ? AND {level_cond}
                  AND CAST(SUBSTR(tourney_date,1,4) AS INTEGER) BETWEEN ? AND ?
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND {ioc_loser}
                {projected_union}
                )
                ORDER BY LOWER(TRIM(player_name)), tourney_date DESC, tourney_info
            )
            GROUP BY LOWER(TRIM(player_name))
            ORDER BY appearances DESC, player_name
        """
        params = (
            [tour, round_] + level_params + [year_from, year_to, ioc, ioc, tour]
            + [tour, round_] + level_params + [year_from, year_to, ioc, ioc, tour]
        ) + projected_params
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_ioc_instance_players(
        self,
        ioc: str,
        tourney_name: str,
        year: int,
        round_: str,
        tour: str = "atp",
    ) -> list[dict]:
        """
        For a specific tournament-year-round instance, return each IOC player
        with their result (W/L) and opponent name.
        """
        ioc_winner = (
            "(winner_ioc = ?"
            " OR (COALESCE(winner_ioc,'')='' AND winner_name IN ("
            "     SELECT name_first||' '||name_last FROM players"
            "     WHERE ioc=? AND tour=?)))"
        )
        ioc_loser = (
            "(loser_ioc = ?"
            " OR (COALESCE(loser_ioc,'')='' AND loser_name IN ("
            "     SELECT name_first||' '||name_last FROM players"
            "     WHERE ioc=? AND tour=?)))"
        )
        sql = f"""
            SELECT player_name, side, opponent_name, score
            FROM (
                SELECT winner_name AS player_name, 'W' AS side,
                       loser_name  AS opponent_name, score
                FROM matches
                WHERE tour = ? AND round = ? AND tourney_name = ?
                  AND CAST(SUBSTR(tourney_date,1,4) AS INTEGER) = ?
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND {ioc_winner}
                UNION ALL
                SELECT loser_name  AS player_name, 'L' AS side,
                       winner_name AS opponent_name, score
                FROM matches
                WHERE tour = ? AND round = ? AND tourney_name = ?
                  AND CAST(SUBSTR(tourney_date,1,4) AS INTEGER) = ?
                  AND (is_upcoming = 0 OR is_upcoming IS NULL)
                  AND {ioc_loser}
            )
            ORDER BY side DESC, player_name
        """
        params = [
            tour, round_, tourney_name, year, ioc, ioc, tour,
            tour, round_, tourney_name, year, ioc, ioc, tour,
        ]
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Name-based queries (for scraped data without player IDs)
    # ------------------------------------------------------------------

    def search_players_by_name(self, name):
        """Find player_id by full or partial name. Returns list of (id, full_name)."""
        cur = self.conn.execute("""
            SELECT player_id, name_first, name_last
            FROM players
            WHERE name_first || ' ' || name_last LIKE ?
            ORDER BY name_last
            LIMIT 10
        """, (f"%{name}%",))
        return [(r[0], f"{r[1]} {r[2]}") for r in cur.fetchall()]

    def _resolve_player_id(self, name):
        """Try to find a player_id matching the given name."""
        if not name:
            return None
        results = self.search_players_by_name(name)
        if results:
            # Prefer exact match
            for pid, full_name in results:
                if full_name.lower() == name.lower():
                    return pid
            return results[0][0]
        return None

    # ------------------------------------------------------------------
    # Scraping integration
    # ------------------------------------------------------------------

    @_locked_write
    def import_scraped_matches(self, matches_df, progress_callback=None,
                               scraped_player_names=None,
                               replace_existing=True):
        """
        Import scraped match data into the database.

        Scraped matches are stored alongside CSV data. We try to resolve
        player names to IDs for compatibility with existing queries.

        Parameters
        ----------
        scraped_player_names : list[str], optional
            Names of the players whose full career was explicitly scraped.
            Old SCRAPED matches for these players will be deleted before
            re-importing.  If None, detected automatically via frequency.
        replace_existing : bool, default True
            If True, delete existing SCRAPED matches for *scraped_player_names*
            before inserting (full refresh).  If False, only insert rows
            that aren't already in the matches table (incremental refresh
            \u2014 use when *matches_df* contains only the most recent N
            matches per player).
        """
        if matches_df is None or matches_df.empty:
            return 0

        if progress_callback:
            progress_callback(0, 1, "Importing scraped matches...")

        # Normalize player names: strip diacritics + replace hyphens with spaces
        # (tennisabstract uses both "Auger-Aliassime" and "Auger Aliassime" for
        # the same player; normalising to spaces ensures consistent storage and
        # correct dedup)
        def _normalize_player_name(name):
            if not isinstance(name, str):
                return name
            return _strip_diacritics(name).replace("-", " ")

        matches_df = matches_df.copy()
        for col in ("winner_name", "loser_name"):
            if col in matches_df.columns:
                matches_df[col] = matches_df[col].apply(_normalize_player_name)

        # Build a name→id and name→ioc cache from existing players.
        # Filter by tour when possible to halve the rows read on remote
        # (Turso) connections — players table holds ATP+WTA combined.
        name_cache = {}
        name_ioc_cache = {}
        canonical_name_by_key = {}
        tours_in_df = set()
        if "tour" in matches_df.columns:
            tours_in_df = {t for t in matches_df["tour"].dropna().unique()
                           if t in ("atp", "wta")}
        if len(tours_in_df) == 1:
            cur = self.conn.execute(
                "SELECT player_id, name_first, name_last, ioc FROM players "
                "WHERE tour = ?", (next(iter(tours_in_df)),))
        else:
            cur = self.conn.execute(
                "SELECT player_id, name_first, name_last, ioc FROM players")
        for row in cur:
            first = row[1] or ""
            last = row[2] or ""
            full = f"{first} {last}".strip()
            if full:
                name_cache[full.lower()] = row[0]
                stripped = f"{_strip_diacritics(first)} {_strip_diacritics(last)}".strip().lower()
                if row[3]:
                    name_ioc_cache[full.lower()] = row[3]
                    if stripped != full.lower():
                        name_ioc_cache[stripped] = row[3]
                # Canonical-name lookup keyed on fully-normalized form so
                # scraped variants (different apostrophe / case / accent)
                # collapse onto the CSV spelling.
                key = _player_match_key(full)
                if key and key not in canonical_name_by_key:
                    canonical_name_by_key[key] = full

        def _canonicalize(name):
            if not isinstance(name, str) or not name:
                return name
            key = _player_match_key(name)
            return canonical_name_by_key.get(key, name)

        # Replace scraped winner/loser names with the canonical CSV spelling
        # whenever the player exists in the players table.  Prevents duplicate
        # match rows that differ only by name spelling.
        for col in ("winner_name", "loser_name"):
            if col in matches_df.columns:
                matches_df[col] = matches_df[col].apply(_canonicalize)

        def resolve(name):
            if not name:
                return ""
            key = name.lower()
            if key in name_cache:
                return name_cache[key]
            # Try last-name-first style
            parts = name.split()
            if len(parts) >= 2:
                alt = " ".join(parts[1:]) + " " + parts[0]
                if alt.lower() in name_cache:
                    return name_cache[alt.lower()]
            return ""

        # Resolve winner/loser IDs
        matches_df = matches_df.copy()
        matches_df["winner_id"] = matches_df["winner_name"].apply(resolve)
        matches_df["loser_id"] = matches_df["loser_name"].apply(resolve)

        # NOTE: we do NOT fill missing ranks at import time.
        # orank values from tennisabstract are the correct rank at the
        # time of the match.  Filling with current scraped rankings would
        # store the WRONG rank for older tournaments.
        # Missing ranks are resolved at query time by _fill_missing_ranks()
        # which uses cross-reference within the tournament, nearby matches,
        # historical CSV rankings, and current scraped rankings (in that
        # order of preference).

        # Fill IOC from players table using the already-built name cache.
        # player_id is NOT unique across ATP/WTA tours, so using it as a
        # cache key causes collisions (e.g. ATP Shelton=USA overwritten by
        # WTA player with same id=COL).  Also, the scraped oioc field is
        # unreliable for recent matches, so we always prefer the players
        # table and ignore any oioc value from the scraped data.

        def fill_ioc_by_name(name):
            if not name:
                return None
            return name_ioc_cache.get(name.lower(), None)

        matches_df["winner_ioc"] = matches_df["winner_name"].apply(fill_ioc_by_name)
        matches_df["loser_ioc"] = matches_df["loser_name"].apply(fill_ioc_by_name)

        # Mark scraped data so we can clear/refresh it separately
        matches_df["tourney_id"] = matches_df["tourney_id"].fillna("")
        matches_df.loc[matches_df["tourney_id"] == "", "tourney_id"] = "SCRAPED"

        # Identify all "main" players being imported.
        # When scraped_player_names is provided (from the scraper), use those
        # directly.  Otherwise fall back to heuristic detection: players whose
        # total appearance count is high enough (within 50 % of the max) are
        # considered scraped players.
        if scraped_player_names:
            scraped_players = set(scraped_player_names)
        else:
            winner_counts = matches_df["winner_name"].value_counts()
            loser_counts = matches_df["loser_name"].value_counts()
            total_counts = winner_counts.add(loser_counts, fill_value=0)
            max_count = total_counts.max()
            # Players with at least 50 % of the max count are scraped players
            scraped_players = set(
                total_counts[total_counts >= max_count * 0.5].index)

        # Canonicalize scraped player names so DELETE matches DB rows
        # (which always store the CSV-canonical spelling, no diacritics).
        # Without this, players whose ranking name has accents
        # (e.g. "Rafael J\u00f3dar") never get their old rows cleared, and
        # the dedup LEFT JOIN below silently drops the new "played"
        # rows because they collide with stale is_upcoming placeholders.
        scraped_players = {_canonicalize(p) for p in scraped_players if p}
        # Remove old scraped matches for ALL scraped players (single query).
        # Skipped when replace_existing=False (incremental mode \u2014 we trust
        # the dedup LEFT JOIN below to avoid duplicates and preserve any
        # older SCRAPED rows already in the DB).
        if scraped_players and replace_existing:
            placeholders = ",".join("?" for _ in scraped_players)
            player_list = list(scraped_players)
            self.conn.execute(
                f"DELETE FROM matches WHERE tourney_id = 'SCRAPED' "
                f"AND (winner_name IN ({placeholders}) "
                f"OR loser_name IN ({placeholders}))",
                player_list + player_list)

        # Always wipe stale upcoming rows for the scraped players, even
        # in incremental mode: when an upcoming match is finally played,
        # winner/loser may swap, so the dedup LEFT JOIN below cannot
        # match the placeholder row.  Re-import rebuilds upcoming list.
        if scraped_players:
            placeholders = ",".join("?" for _ in scraped_players)
            player_list = list(scraped_players)
            try:
                self.conn.execute(
                    f"DELETE FROM matches WHERE is_upcoming = 1 "
                    f"AND (winner_name IN ({placeholders}) "
                    f"OR loser_name IN ({placeholders}))",
                    player_list + player_list)
            except sqlite3.OperationalError:
                pass  # column missing on first run before migration

        # Drop "upcoming" rows whose date is already in the past.  These
        # are stale placeholders left behind on tennisabstract when a
        # match was played but the result hasn't been published yet.
        # Importing them would re-create the same stale upcoming rows we
        # just deleted above, leaving the matches stuck in "upcoming"
        # forever in the UI.
        if "is_upcoming" in matches_df.columns and \
                "tourney_date" in matches_df.columns:
            from datetime import datetime, timezone
            today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
            stale_mask = (
                (matches_df["is_upcoming"] == 1)
                & (matches_df["tourney_date"].astype(str) < today_str)
            )
            n_stale = int(stale_mask.sum())
            if n_stale:
                logger.info(
                    "Dropping %d stale upcoming rows (date < %s)",
                    n_stale, today_str)
                matches_df = matches_df[~stale_mask].copy()

        # Remove duplicates within the DataFrame (same date + winner + loser + tourney)
        matches_df = matches_df.drop_duplicates(
            subset=["tourney_date", "winner_name", "loser_name", "tourney_name", "round"],
            keep="first",
        )

        # Avoid inserting matches that already exist (CSV or other scrapes).
        # Use a temp table + LEFT JOIN instead of loading all 1.7M rows into Python.
        # Lowercased tourney_name avoids duplicates like "Us Open" vs "US Open".
        # Unique staging name (UUID) so concurrent imports cannot collide.
        staging_name = f"_import_staging_{uuid.uuid4().hex[:12]}"
        # On remote (Turso) connections the matches table contains only
        # SCRAPED rows — no historical CSV imports — so we can scope the
        # dedup JOIN to SCRAPED to massively reduce rows read.
        join_extra = ""
        if _is_remote_conn(self.conn):
            join_extra = " AND m.tourney_id = 'SCRAPED'"
        try:
            matches_df.to_sql(staging_name, self.conn, if_exists="replace", index=False)
            self.conn.execute(f"""
                DELETE FROM {staging_name}
                WHERE rowid IN (
                    SELECT s.rowid
                    FROM {staging_name} s
                    JOIN matches m
                      ON SUBSTR(m.tourney_date, 1, 4) = SUBSTR(s.tourney_date, 1, 4)
                     AND m.winner_name = s.winner_name
                     AND m.loser_name = s.loser_name
                     AND LOWER(m.tourney_name) = LOWER(s.tourney_name)
                     AND m.round = s.round{join_extra}
                )
            """)
            count_row = self.conn.execute(
                f"SELECT COUNT(*) FROM {staging_name}").fetchone()
            new_count = count_row[0]
            if new_count > 0:
                staging_cols = [
                    row[1] for row in
                    self.conn.execute(f"PRAGMA table_info({staging_name})").fetchall()
                ]
                cols_csv = ", ".join(staging_cols)
                self.conn.execute(f"""
                    INSERT INTO matches ({cols_csv})
                    SELECT {cols_csv} FROM {staging_name}
                """)
                logger.info("Imported %d new scraped matches", new_count)
            else:
                logger.info("No new scraped matches to import")
        finally:
            self.conn.execute(f"DROP TABLE IF EXISTS {staging_name}")

        self.conn.commit()

        if progress_callback:
            progress_callback(1, 1, "Scraped matches imported!")

        return new_count

    @_locked_write
    def import_scraped_rankings(self, rankings_list, ranking_date="LIVE",
                                progress_callback=None):
        """
        Import scraped rankings from live-tennis/tennisabstract.
        
        ranking_date identifies the scraped snapshot (e.g. LIVE,
        SCRAPED_OFFICIAL_SINGLES, ...).
        """
        if not rankings_list:
            return 0

        if progress_callback:
            progress_callback(0, 1, "Importing scraped rankings...")

        tour = rankings_list[0].get("tour", "atp")

        # Remove previous snapshot for this tour+marker
        self.conn.execute(
            "DELETE FROM rankings WHERE ranking_date = ? AND tour = ?",
            (ranking_date, tour),
        )

        # Build name→id cache once (instead of per-player query)
        # Use diacritics-stripped keys so "Đoković" matches "Djokovic" etc.
        def _normalize_name(s):
            # Strip diacritics
            s = "".join(
                c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn"
            )
            # Normalise hyphens/punctuation to spaces
            s = s.replace("-", " ").replace("'", "")
            # Collapse whitespace
            return " ".join(s.split()).lower()

        name_cache = {}
        cur = self.conn.execute(
            "SELECT player_id, name_first, name_last FROM players "
            "WHERE tour = ?", (tour,))
        for row in cur:
            first = row[1] or ""
            last = row[2] or ""
            full = f"{first} {last}".strip()
            if full:
                name_cache[full.lower()] = row[0]
                norm = _normalize_name(full)
                if norm not in name_cache:
                    name_cache[norm] = row[0]

        records = []
        for entry in rankings_list:
            name = entry["name"]
            player_id = name_cache.get(name.lower())
            if player_id is None:
                player_id = name_cache.get(
                    _normalize_name(name), name)
            records.append({
                "ranking_date": ranking_date,
                "rank": entry["rank"],
                "player_id": player_id,
                "points": entry["points"],
                "tour": entry.get("tour", "atp"),
                "age": entry.get("age"),
                "rank_diff": entry.get("rank_diff"),
                "pts_diff": entry.get("pts_diff"),
                "next_tournament": entry.get("next_tournament", ""),
                "ioc": entry.get("country", ""),
            })

        if records:
            df = pd.DataFrame(records)
            df.to_sql("rankings", self.conn, if_exists="append", index=False)

        self.conn.commit()

        if progress_callback:
            progress_callback(1, 1, "Live rankings imported!")

        return len(records)

    @staticmethod
    def scraped_ranking_marker(source="LIVE", discipline="singles"):
        src = (source or "LIVE").upper()
        disc = (discipline or "singles").upper()
        return f"SCRAPED_{src}_{disc}"

    def refresh_scraped_rankings(self, tour="atp", discipline="singles",
                                 source="LIVE"):
        """Fetch + import one scraped ranking snapshot and return marker."""
        rankings = scrape_current_rankings(
            tour=tour,
            discipline=discipline,
            source=source,
        )
        marker = self.scraped_ranking_marker(source=source,
                                             discipline=discipline)
        if not rankings:
            # Keep DB clean if fetch failed.
            self.conn.execute(
                "DELETE FROM rankings WHERE ranking_date = ? AND tour = ?",
                (marker, tour),
            )
            self.conn.commit()
            return marker, 0

        count = self.import_scraped_rankings(rankings, ranking_date=marker)

        # Persist OFFICIAL singles snapshot also under a real Monday-of-week
        # date so it shows up on the player ranking-history charts and slowly
        # accumulates a weekly history (the upstream Sackmann CSVs stop at
        # 2024-12-30, so without this our charts would have no 2025+ data).
        if (source or "").upper() == "OFFICIAL" \
                and (discipline or "singles").lower() == "singles":
            try:
                self._promote_marker_to_dated_snapshot(tour, marker)
            except Exception:
                logger.exception(
                    "Failed to promote %s snapshot to dated row", marker)

        return marker, count

    def _promote_marker_to_dated_snapshot(self, tour, marker):
        """Copy a marker-keyed ranking snapshot under today's Monday date.

        Idempotent: replaces any existing rows for that (tour, monday).
        """
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        monday_str = monday.strftime("%Y%m%d")
        cur = self.conn.execute(
            "DELETE FROM rankings WHERE tour = ? AND ranking_date = ?",
            (tour, monday_str),
        )
        self.conn.execute("""
            INSERT INTO rankings
                (ranking_date, rank, player_id, points, tour, age,
                 rank_diff, pts_diff, next_tournament, ioc)
            SELECT ?, rank, player_id, points, tour, age,
                   rank_diff, pts_diff, next_tournament, ioc
            FROM rankings
            WHERE tour = ? AND ranking_date = ?
        """, (monday_str, tour, marker))
        self.conn.commit()
        logger.info("Promoted %s snapshot to ranking_date=%s (%s)",
                    marker, monday_str, tour)

    def backfill_official_snapshots_to_today(self):
        """Promote any existing OFFICIAL singles markers to today's Monday.

        One-shot helper to seed the ranking history charts immediately
        without waiting for the next scheduled scrape.  Idempotent.
        Returns dict {tour: rows_copied}.
        """
        out = {}
        for tour in ("atp", "wta"):
            marker = self.scraped_ranking_marker(source="OFFICIAL",
                                                 discipline="singles")
            cnt = self.conn.execute(
                "SELECT COUNT(*) FROM rankings WHERE tour=? AND ranking_date=?",
                (tour, marker),
            ).fetchone()[0]
            if cnt:
                self._promote_marker_to_dated_snapshot(tour, marker)
                out[tour] = cnt
            else:
                out[tour] = 0
        return out

    def backfill_rankings_history(self, tour, start_date, end_date,
                                  top_n=500, sleep_s=2.0,
                                  progress_callback=None):
        """Back-fill weekly historical ranking snapshots from third-party
        sources to fill the gap between the frozen Sackmann CSVs (last
        week 2024-12-30) and the current scraped snapshot.

        Parameters
        ----------
        tour : str
            'atp' (uses atptour.com) or 'wta' (uses tennisexplorer.com).
        start_date, end_date : str | datetime.date
            Inclusive date range.  Iterates by week (Mondays only).
            Strings must be 'YYYY-MM-DD'.
        top_n : int
            Number of ranks to fetch per snapshot (default 500).
        sleep_s : float
            Delay between snapshot fetches (per Monday).

        Idempotent: skips dates already present in the DB for this tour.
        Returns dict {date_str: rows_inserted}.
        """
        from .scraper import TennisAbstractScraper

        if isinstance(start_date, str):
            start = date.fromisoformat(start_date)
        else:
            start = start_date
        if isinstance(end_date, str):
            end = date.fromisoformat(end_date)
        else:
            end = end_date

        # Snap to Monday
        start = start - timedelta(days=start.weekday())
        end = end - timedelta(days=end.weekday())

        scraper = TennisAbstractScraper()

        # Build the list of weeks to fetch.
        # ATP doesn't publish every Monday (off-season weeks skipped),
        # so query atptour.com once for the list of valid date_week
        # values and intersect with the requested range.  WTA via TE
        # accepts any Monday but may return empty on off-weeks; we
        # accept that as "no data".
        if tour == "atp":
            try:
                weeks = self._fetch_atp_available_weeks()
                weeks = [d for d in weeks if start <= d <= end]
                weeks.sort()
                logger.info("[backfill atp] %d valid ATP ranking weeks "
                            "in [%s..%s]", len(weeks),
                            start.isoformat(), end.isoformat())
            except Exception as exc:
                logger.warning("[backfill atp] week-list fetch failed "
                               "(%s); falling back to every Monday", exc)
                weeks = []
                d = start
                while d <= end:
                    weeks.append(d)
                    d += timedelta(days=7)
        else:
            weeks = []
            d = start
            while d <= end:
                weeks.append(d)
                d += timedelta(days=7)

        out = {}
        total_weeks = len(weeks)
        for wk_idx, cur_d in enumerate(weeks, 1):
            iso = cur_d.isoformat()
            yyyymmdd = cur_d.strftime("%Y%m%d")
            # Skip if already present
            existing = self.conn.execute(
                "SELECT COUNT(*) FROM rankings "
                "WHERE tour=? AND ranking_date=?",
                (tour, yyyymmdd),
            ).fetchone()[0]
            if existing:
                logger.info("[backfill %s] %s: %d rows already present, skip",
                            tour, iso, existing)
                out[iso] = 0
                if progress_callback:
                    progress_callback(wk_idx, total_weeks,
                                      f"{iso}: skipped")
                continue

            if tour == "atp":
                rows = scraper.scrape_atp_history_snapshot(iso, top_n=top_n)
            elif tour == "wta":
                rows = scraper.scrape_wta_history_snapshot(iso, top_n=top_n)
            else:
                raise ValueError(f"Unsupported tour: {tour}")

            if not rows:
                logger.warning("[backfill %s] %s: no rows scraped", tour, iso)
                out[iso] = 0
            else:
                # Reuse existing import path (handles name→player_id)
                inserted = self.import_scraped_rankings(
                    rows, ranking_date=yyyymmdd)
                out[iso] = inserted
                logger.info("[backfill %s] %s: %d rows imported",
                            tour, iso, inserted)

            if progress_callback:
                progress_callback(wk_idx, total_weeks,
                                  f"{iso}: {out[iso]} rows")

            if wk_idx < total_weeks and sleep_s > 0:
                import time as _t
                _t.sleep(sleep_s)
        return out

    def _fetch_atp_available_weeks(self):
        """Return a sorted list of date objects for which atptour.com
        publishes a singles ranking (extracted from the date dropdown).
        """
        import re as _re
        import cloudscraper as _cs
        from bs4 import BeautifulSoup as _BS
        sc = _cs.create_scraper()
        resp = sc.get("https://www.atptour.com/en/rankings/singles",
                      timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"ATP rankings page status "
                               f"{resp.status_code}")
        soup = _BS(resp.text, "html.parser")
        seen = set()
        for o in soup.find_all("option"):
            val = (o.get("value") or "").strip()
            if _re.match(r"^\d{4}-\d{2}-\d{2}$", val):
                seen.add(val)
        out = []
        for s in seen:
            try:
                out.append(date.fromisoformat(s))
            except ValueError:
                continue
        out.sort()
        return out

    def get_scraped_match_count(self):
        """Return how many scraped matches are in the database."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM matches WHERE tourney_id = 'SCRAPED'")
        return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Scrape cache management
    # ------------------------------------------------------------------

    def is_player_cache_valid(self, player_name, expire_hours=6):
        """Check if a player's scraped data is still fresh.

        Simple time-based check: returns True if the player was scraped
        less than *expire_hours* ago.  The caller is responsible for
        choosing an appropriate *expire_hours* (e.g. via activity
        fingerprint comparison).

        *expire_hours* = 0 always forces a refresh.
        """
        row = self.conn.execute(
            "SELECT last_scraped FROM scrape_cache "
            "WHERE player_name = ?",
            (player_name,)
        ).fetchone()
        if not row:
            return False
        if expire_hours == 0:
            return False
        from datetime import datetime, timedelta
        try:
            last = datetime.fromisoformat(row[0])
            return datetime.now() - last < timedelta(hours=expire_hours)
        except (ValueError, TypeError):
            return False

    def has_new_activity(self, player_name, new_fingerprint):
        """Return True if the player's activity fingerprint has changed.

        The OFFICIAL rankings "previous" column concatenates results
        from the last 1-2 weeks (e.g. ``"Lost in MC R64Lost in BCN R2"``).
        When old results drop off, the string shrinks but no new match
        data exists.

        To avoid false positives we check whether the *new* current and
        previous texts already appear inside the *old* combined
        fingerprint.  Only truly new text triggers a re-scrape.

        Returns True (needs scraping) when:
        - player not in cache
        - new current or previous text is NOT already present in the
          stored fingerprint (genuinely new result)
        Returns False (skip) when:
        - fingerprints match exactly
        - new text is a subset of the old combined text (old results
          just dropped off, or current moved to previous)
        - a field merely cleared (went from something to empty)
        """
        row = self.conn.execute(
            "SELECT activity_fingerprint FROM scrape_cache "
            "WHERE player_name = ?",
            (player_name,)
        ).fetchone()
        if not row or row[0] is None:
            return True  # never scraped or no fingerprint stored
        old_fp = row[0]
        if old_fp == new_fingerprint:
            return False
        # Combine old current+previous into one reference string.
        old_combined = old_fp.replace("|", "")
        new_cur, new_prev = (new_fingerprint.split("|", 1) + [""])[:2]
        # Only trigger if a non-empty new part is NOT already present
        # in the old combined text.
        cur_is_new = bool(new_cur) and new_cur not in old_combined
        prev_is_new = bool(new_prev) and new_prev not in old_combined
        return cur_is_new or prev_is_new

    @_locked_write
    def update_scrape_cache(self, player_name, match_count,
                            last_match_date=None,
                            match_signature=None,
                            activity_fingerprint=None,
                            scrape_retry_count=None,
                            next_scrape_after=None):
        """Record that a player was just scraped.

        If *activity_fingerprint* is None, the existing stored
        fingerprint is preserved (only ``last_scraped`` and
        ``match_count`` are updated).  Callers pass None when a scrape
        did not confirm the new activity yet (for example because
        tennisabstract still has not published the latest result);
        preserving the old fingerprint ensures the next run will detect
        the same activity change and retry.
        """
        from datetime import datetime
        now_iso = datetime.now().isoformat()
        if activity_fingerprint is None:
            self.conn.execute(
                "INSERT INTO scrape_cache "
                "(player_name, last_scraped, match_count, "
                "last_match_date, activity_fingerprint, match_signature) "
                "VALUES (?, ?, ?, ?, NULL, ?) "
                "ON CONFLICT(player_name) DO UPDATE SET "
                "last_scraped = excluded.last_scraped, "
                "match_count = excluded.match_count, "
                "last_match_date = COALESCE(excluded.last_match_date, "
                "                          scrape_cache.last_match_date), "
                "match_signature = COALESCE(excluded.match_signature, "
                "                           scrape_cache.match_signature), "
                "scrape_retry_count = COALESCE(?, "
                "                              scrape_cache.scrape_retry_count), "
                "next_scrape_after = COALESCE(?, "
                "                             scrape_cache.next_scrape_after)",
                (player_name, now_iso, match_count, last_match_date,
                 match_signature, scrape_retry_count, next_scrape_after)
            )
        else:
            self.conn.execute(
                "INSERT INTO scrape_cache "
                "(player_name, last_scraped, match_count, last_match_date, "
                "activity_fingerprint, match_signature, scrape_retry_count, "
                "next_scrape_after) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, NULL) "
                "ON CONFLICT(player_name) DO UPDATE SET "
                "last_scraped = excluded.last_scraped, "
                "match_count = excluded.match_count, "
                "last_match_date = excluded.last_match_date, "
                "activity_fingerprint = excluded.activity_fingerprint, "
                "match_signature = excluded.match_signature, "
                "scrape_retry_count = 0, "
                "next_scrape_after = NULL",
                (player_name, now_iso, match_count,
                 last_match_date, activity_fingerprint, match_signature)
            )
        self.conn.commit()

    def get_stale_players(self, player_names, expire_hours=6):
        """Return the subset of player_names whose cache is expired or missing."""
        if not player_names:
            return []
        stale = []
        for name in player_names:
            if not self.is_player_cache_valid(name, expire_hours):
                stale.append(name)
        return stale

    def get_all_scrape_cache(self):
        """Bulk-load the entire scrape_cache table.

        Returns a dict
        ``{player_name: (last_scraped_iso, fingerprint, last_match_date,
        match_signature, retry_count, next_scrape_after)}``.
        Used to avoid 1000+ sequential round-trips when checking activity
        for top-N rankings against a remote (Turso) database.
        """
        rows = self.conn.execute(
            "SELECT player_name, last_scraped, activity_fingerprint, "
            "last_match_date, match_signature, scrape_retry_count, "
            "next_scrape_after "
            "FROM scrape_cache"
        ).fetchall()
        return {r[0]: (r[1], r[2], r[3], r[4], r[5] or 0, r[6])
                for r in rows}

    def get_players_with_due_upcoming(self, today_yyyymmdd: str | None = None):
        """Return the set of player names that have at least one match
        currently flagged ``is_upcoming = 1`` whose ``tourney_date`` is
        on/before *today*.

        These players need a re-scrape because their "upcoming" rows
        should now have a completed result on tennisabstract.

        ``today_yyyymmdd`` defaults to UTC today.
        """
        if today_yyyymmdd is None:
            from datetime import datetime, timezone
            today_yyyymmdd = datetime.now(timezone.utc).strftime("%Y%m%d")
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT winner_name FROM matches "
                "WHERE is_upcoming = 1 AND tourney_date <= ? "
                "UNION "
                "SELECT DISTINCT loser_name FROM matches "
                "WHERE is_upcoming = 1 AND tourney_date <= ?",
                (today_yyyymmdd, today_yyyymmdd),
            ).fetchall()
        except sqlite3.OperationalError:
            # is_upcoming column doesn't exist yet (pre-migration)
            return set()
        return {r[0] for r in rows if r[0]}

    # ------------------------------------------------------------------
    # Extended stats queries
    # ------------------------------------------------------------------

    def _build_ext_filters(self, player_name, surface, year,
                           tourney_level, round_):
        """Build the WHERE clause + params shared by extended-stats getters.

        ``tourney_level`` and ``round_`` may be a scalar or an iterable of
        scalars (multi-select).
        """
        conditions = ["player_name = ?"]
        params = [player_name]
        if surface:
            conditions.append("surface = ?")
            params.append(surface)
        if year:
            conditions.append("tourney_date BETWEEN ? AND ?")
            params.extend([f"{year}0000", f"{year}9999"])
        _eq_or_in("tourney_level", tourney_level, conditions, params)
        _eq_or_in("round", round_, conditions, params)
        return " AND ".join(conditions), params

    def get_player_winners_errors(self, player_name, surface=None, year=None,
                                  tourney_level=None, round_=None):
        """Get winners/errors data for a player."""
        where, params = self._build_ext_filters(
            player_name, surface, year, tourney_level, round_)
        cur = self.conn.execute(
            f"SELECT * FROM match_winners_errors WHERE {where} "
            "ORDER BY tourney_date DESC", params)
        return [dict(r) for r in cur.fetchall()]

    def get_player_serve_speed(self, player_name, surface=None, year=None,
                               tourney_level=None, round_=None):
        """Get serve speed data for a player."""
        where, params = self._build_ext_filters(
            player_name, surface, year, tourney_level, round_)
        cur = self.conn.execute(
            f"SELECT * FROM match_serve_speed WHERE {where} "
            "ORDER BY tourney_date DESC", params)
        return [dict(r) for r in cur.fetchall()]

    def get_player_pbp_stats(self, player_name, surface=None, year=None,
                             tourney_level=None, round_=None):
        """Get point-by-point stats for a player."""
        where, params = self._build_ext_filters(
            player_name, surface, year, tourney_level, round_)
        cur = self.conn.execute(
            f"SELECT * FROM match_pbp_stats WHERE {where} "
            "ORDER BY tourney_date DESC", params)
        return [dict(r) for r in cur.fetchall()]

    def get_player_mcp_serve(self, player_name, surface=None, year=None,
                             tourney_level=None, round_=None):
        """Get MCP serve direction data for a player."""
        where, params = self._build_ext_filters(
            player_name, surface, year, tourney_level, round_)
        cur = self.conn.execute(
            f"SELECT * FROM match_mcp_serve WHERE {where} "
            "ORDER BY tourney_date DESC", params)
        return [dict(r) for r in cur.fetchall()]

    def get_player_mcp_return(self, player_name, surface=None, year=None,
                              tourney_level=None, round_=None):
        """Get MCP return data for a player."""
        where, params = self._build_ext_filters(
            player_name, surface, year, tourney_level, round_)
        cur = self.conn.execute(
            f"SELECT * FROM match_mcp_return WHERE {where} "
            "ORDER BY tourney_date DESC", params)
        return [dict(r) for r in cur.fetchall()]

    def get_player_mcp_rally(self, player_name, surface=None, year=None,
                             tourney_level=None, round_=None):
        """Get MCP rally data for a player."""
        where, params = self._build_ext_filters(
            player_name, surface, year, tourney_level, round_)
        cur = self.conn.execute(
            f"SELECT * FROM match_mcp_rally WHERE {where} "
            "ORDER BY tourney_date DESC", params)
        return [dict(r) for r in cur.fetchall()]

    def get_player_mcp_tactics(self, player_name, surface=None, year=None,
                               tourney_level=None, round_=None):
        """Get MCP tactics data for a player."""
        where, params = self._build_ext_filters(
            player_name, surface, year, tourney_level, round_)
        cur = self.conn.execute(
            f"SELECT * FROM match_mcp_tactics WHERE {where} "
            "ORDER BY tourney_date DESC", params)
        return [dict(r) for r in cur.fetchall()]

    def _migrate_normalize_extended_player_names(self, cur):
        """Rewrite ``player_name`` in extended-stats tables and cache to
        the diacritic-free spelling produced by ``clean_player_name``.

        Idempotent: rows already in canonical form are skipped.  When a
        canonical row already exists for the same player, the legacy
        accented row is deleted to avoid PRIMARY KEY conflicts in the
        cache table.
        """
        if _is_remote_conn(self.conn):
            return  # too slow over Turso HTTP; ran on local DB already
        from .scraper import clean_player_name

        tables = [
            "match_winners_errors", "match_serve_speed", "match_pbp_stats",
            "match_mcp_serve", "match_mcp_return", "match_mcp_rally",
            "match_mcp_tactics", "extended_stats_cache",
        ]
        total_updated = 0
        for tbl in tables:
            try:
                rows = cur.execute(
                    f"SELECT DISTINCT player_name FROM {tbl}"
                ).fetchall()
            except sqlite3.OperationalError:
                continue  # table doesn't exist yet
            for (raw,) in rows:
                if not raw:
                    continue
                canonical = clean_player_name(raw)
                if not canonical or canonical == raw:
                    continue
                if tbl == "extended_stats_cache":
                    # PK conflict possible — delete legacy if canonical exists
                    exists = cur.execute(
                        f"SELECT 1 FROM {tbl} WHERE player_name = ?",
                        (canonical,)).fetchone()
                    if exists:
                        cur.execute(
                            f"DELETE FROM {tbl} WHERE player_name = ?",
                            (raw,))
                    else:
                        cur.execute(
                            f"UPDATE {tbl} SET player_name = ? "
                            f"WHERE player_name = ?",
                            (canonical, raw))
                else:
                    cur.execute(
                        f"UPDATE {tbl} SET player_name = ? "
                        f"WHERE player_name = ?",
                        (canonical, raw))
                total_updated += 1
        if total_updated:
            self.conn.commit()
            logger.info(
                "Normalized %d distinct accented player_name entries "
                "across extended-stats tables", total_updated)

    def _migrate_normalize_match_player_names(self, cur):
        """Collapse name-spelling duplicates in the ``matches`` table.

        Two-phase, idempotent:

        1. Build a canonical-name map from the ``players`` table keyed on
           the diacritic / case / apostrophe / hyphen-insensitive form
           (see ``_player_match_key``).  Update every ``winner_name`` /
           ``loser_name`` whose canonical form differs.

        2. Delete duplicate rows that differ only by spelling, grouping
           on (tour, tourney_id, tourney_date, round, winner_name,
           loser_name).  In each group keep the row with the most
           informative ``score`` (longest non-NULL), preferring
           ``is_upcoming = 0`` over scheduled rows.
        """
        if _is_remote_conn(self.conn):
            return  # too slow over Turso HTTP; ran on local DB already
        # Phase 1: build canonical lookup
        canonical_by_key = {}
        for row in cur.execute(
                "SELECT name_first, name_last FROM players"):
            first, last = row[0] or "", row[1] or ""
            full = f"{first} {last}".strip()
            if not full:
                continue
            key = _player_match_key(full)
            if key and key not in canonical_by_key:
                canonical_by_key[key] = full

        if not canonical_by_key:
            return

        # Collect distinct names actually present in matches
        distinct_names = set()
        try:
            for (n,) in cur.execute(
                    "SELECT DISTINCT winner_name FROM matches "
                    "WHERE winner_name IS NOT NULL"):
                distinct_names.add(n)
            for (n,) in cur.execute(
                    "SELECT DISTINCT loser_name FROM matches "
                    "WHERE loser_name IS NOT NULL"):
                distinct_names.add(n)
        except sqlite3.OperationalError:
            return

        rename_pairs = []  # (old, new)
        for raw in distinct_names:
            if not raw:
                continue
            key = _player_match_key(raw)
            canonical = canonical_by_key.get(key)
            if canonical and canonical != raw:
                rename_pairs.append((raw, canonical))

        for old, new in rename_pairs:
            cur.execute(
                "UPDATE matches SET winner_name = ? WHERE winner_name = ?",
                (new, old))
            cur.execute(
                "UPDATE matches SET loser_name = ? WHERE loser_name = ?",
                (new, old))

        # Phase 2: dedup.  Group by the natural identity of a match.
        dup_groups = cur.execute("""
            SELECT tour, tourney_id, tourney_date, round,
                   winner_name, loser_name, COUNT(*) AS cnt
            FROM matches
            WHERE winner_name IS NOT NULL AND loser_name IS NOT NULL
              AND tourney_date IS NOT NULL AND round IS NOT NULL
            GROUP BY tour, tourney_id, tourney_date, round,
                     winner_name, loser_name
            HAVING COUNT(*) > 1
        """).fetchall()

        deleted = 0
        for tour, tid, tdate, rnd, wn, ln, _cnt in dup_groups:
            rows = cur.execute("""
                SELECT rowid, score, is_upcoming
                FROM matches
                WHERE tour IS ? AND tourney_id IS ? AND tourney_date IS ?
                  AND round IS ? AND winner_name = ? AND loser_name = ?
            """, (tour, tid, tdate, rnd, wn, ln)).fetchall()
            if len(rows) <= 1:
                continue

            def quality(r):
                score = r[1] or ""
                upcoming = r[2] or 0
                # Prefer non-upcoming, then longer score
                return (0 if upcoming else 1, len(score))

            rows.sort(key=quality, reverse=True)
            keep = rows[0][0]
            for r in rows[1:]:
                cur.execute("DELETE FROM matches WHERE rowid = ?", (r[0],))
                deleted += 1

        if rename_pairs or deleted:
            self.conn.commit()
            logger.debug(
                "Normalized %d matches.player_name spellings; "
                "deleted %d duplicate match rows",
                len(rename_pairs), deleted)

    def _migrate_fix_scraped_match_tour(self, cur):
        """Fix scraped matches that were saved with tour='atp' regardless of
        the actual tour.

        Strategy: a match is WTA if BOTH players are exclusively in the WTA
        players table (i.e. present in wta but absent in atp).  We build two
        name sets from the players table and use them to relabel scraped rows.

        Idempotent: only touches rows with tourney_id='SCRAPED' and tour='atp'
        that can be positively identified as WTA.
        """
        if _is_remote_conn(self.conn):
            return  # too slow over Turso HTTP; ran on local DB already
        # Build name sets per tour from the players table
        wta_names = set()
        atp_names = set()
        for row in cur.execute(
                "SELECT name_first, name_last, tour FROM players "
                "WHERE tour IN ('atp', 'wta')"):
            first, last, t = row[0] or "", row[1] or "", row[2] or ""
            full = f"{first} {last}".strip()
            if not full:
                continue
            key = _player_match_key(full)
            if t == "wta":
                wta_names.add(key)
            else:
                atp_names.add(key)

        if not wta_names:
            return

        # Collect distinct (winner_name, loser_name) pairs for scraped rows
        # stored as 'atp' tour — these are the candidates to fix.
        try:
            candidates = cur.execute("""
                SELECT DISTINCT winner_name, loser_name
                FROM matches
                WHERE tourney_id = 'SCRAPED' AND tour = 'atp'
                  AND winner_name IS NOT NULL AND loser_name IS NOT NULL
            """).fetchall()
        except Exception:
            return

        updated_pairs = 0
        for winner_name, loser_name in candidates:
            wk = _player_match_key(winner_name)
            lk = _player_match_key(loser_name)
            w_is_wta = wk in wta_names and wk not in atp_names
            l_is_wta = lk in wta_names and lk not in atp_names
            # Mark as WTA only when at least one player is WTA-exclusive.
            # (Slams / mixed events: both players may appear in both tables —
            # those stay as-is since they're usually ATP data or ambiguous.)
            if w_is_wta or l_is_wta:
                cur.execute("""
                    UPDATE matches SET tour = 'wta'
                    WHERE tourney_id = 'SCRAPED' AND tour = 'atp'
                      AND winner_name = ? AND loser_name = ?
                """, (winner_name, loser_name))
                updated_pairs += 1

        if updated_pairs:
            self.conn.commit()
            logger.info(
                "Fixed tour field for %d scraped WTA match pairs "
                "(was 'atp', now 'wta')", updated_pairs)

    def get_extended_stats_count(self, player_name):
        """Return count of extended stats records per table for a player."""
        tables = [
            "match_winners_errors", "match_serve_speed", "match_pbp_stats",
            "match_mcp_serve", "match_mcp_return", "match_mcp_rally",
            "match_mcp_tactics",
        ]
        # Single query with UNION ALL instead of 7 separate queries
        parts = [
            f"SELECT '{t}' as tbl, COUNT(*) as cnt FROM {t} WHERE player_name = ?"
            for t in tables
        ]
        query = " UNION ALL ".join(parts)
        cur = self.conn.execute(query, [player_name] * len(tables))
        return {row[0]: row[1] for row in cur.fetchall()}

    def get_match_extended_stats(self, player_name: str, opponent_name: str,
                                 tourney_name: str, tourney_date: str) -> dict:
        """Return extended stats rows for a single match.

        Tries the exact names first, then retries with ``clean_player_name``
        normalization so scraper-side spellings always match stored data.

        Returns a dict with 7 keys, each the first matching row as a dict
        (or None if no row found):
        ``winners_errors``, ``serve_speed``, ``pbp``,
        ``mcp_serve``, ``mcp_return``, ``mcp_rally``, ``mcp_tactics``.
        """
        from .scraper import clean_player_name

        p = clean_player_name(player_name) or player_name
        o = clean_player_name(opponent_name) or opponent_name
        tn = clean_player_name(tourney_name) or tourney_name  # keep as-is

        # tourney_name is not normalised via clean_player_name; use original.
        tn = tourney_name

        tables_keys = [
            ("match_winners_errors", "winners_errors"),
            ("match_serve_speed", "serve_speed"),
            ("match_pbp_stats", "pbp"),
            ("match_mcp_serve", "mcp_serve"),
            ("match_mcp_return", "mcp_return"),
            ("match_mcp_rally", "mcp_rally"),
            ("match_mcp_tactics", "mcp_tactics"),
        ]

        result = {}
        for tbl, key in tables_keys:
            row = None
            # Try player p (winner/loser perspective) vs opponent o
            for pn, on in [(p, o), (o, p)]:
                cur = self.conn.execute(
                    f"SELECT * FROM {tbl} "
                    f"WHERE player_name = ? AND opponent_name = ? "
                    f"  AND tourney_name = ? AND tourney_date = ? "
                    f"LIMIT 1",
                    (pn, on, tn, str(tourney_date)),
                )
                row = cur.fetchone()
                if row:
                    row = dict(row)
                    row["_perspective"] = "player" if pn == p else "opponent"
                    break
            result[key] = row
        return result

    @_locked_write
    def import_extended_stats(self, table_name, records, player_name):
        """Import extended stats records, replacing existing data for the player."""
        if not records:
            return 0
        # Clear existing records for this player in this table
        self.conn.execute(
            f"DELETE FROM {table_name} WHERE player_name = ?",
            (player_name,))
        import pandas as pd
        df = pd.DataFrame(records)
        df.to_sql(table_name, self.conn, if_exists="append", index=False)

        # Back-fill surface and tourney_level using in-memory lookup.
        # Skip on remote (Turso) connections: this scans large slices of
        # the matches table per player × per stats table — millions of
        # rows_read per scrape run.  Desktop clients can call
        # ``backfill_extended_stats_surface()`` once after a snapshot
        # refresh to populate these columns offline.
        if _is_remote_conn(self.conn):
            self.conn.commit()
            return len(records)

        rows = self.conn.execute(
            f"SELECT id, tourney_name, tourney_date, opponent_name "
            f"FROM {table_name} "
            f"WHERE player_name = ? AND ("
            f"  surface IS NULL OR surface = '' "
            f"  OR tourney_level IS NULL OR tourney_level = '')",
            (player_name,)).fetchall()
        if rows:
            # Collect years we need, then build lookup in one query
            years = {str(r[2])[:4] for r in rows if r[2]}
            if years:
                # Build range conditions for each year (index-friendly)
                range_conds = " OR ".join(
                    "tourney_date BETWEEN ? AND ?" for _ in years)
                range_params = []
                for y in years:
                    range_params.extend([f"{y}0000", f"{y}9999"])
                match_rows = self.conn.execute(
                    f"SELECT tourney_name, tourney_date, winner_name, "
                    f"loser_name, surface, tourney_level FROM matches "
                    f"WHERE {range_conds}",
                    range_params).fetchall()
                # Build lookup: (year, last_name) -> [(tourney_name, surface, level)]
                lookup = {}
                for mtn, mtd, wn, ln, surf, lvl in match_rows:
                    year = str(mtd)[:4] if mtd else ""
                    for full_name in (wn, ln):
                        if not full_name:
                            continue
                        last = full_name.rsplit(" ", 1)[-1] if " " in full_name else full_name
                        key = (year, last)
                        if key not in lookup:
                            lookup[key] = []
                        lookup[key].append((mtn or "", surf, lvl))

                updates = []
                for rid, etn, etd, opp in rows:
                    year = str(etd)[:4] if etd else ""
                    last = opp.rsplit(" ", 1)[-1] if opp and " " in opp else (opp or "")
                    candidates = lookup.get((year, last), [])
                    surf = lvl = None
                    for mtn, ms, ml in candidates:
                        if etn and etn in mtn:
                            surf, lvl = ms, ml
                            break
                    if surf is None:
                        for mtn, ms, ml in candidates:
                            if etn and mtn and (etn in mtn or mtn in etn):
                                surf, lvl = ms, ml
                                break
                    if surf or lvl:
                        updates.append((surf, lvl, rid))
                if updates:
                    self.conn.executemany(
                        f"UPDATE {table_name} SET surface = COALESCE(?, surface), "
                        f"tourney_level = COALESCE(?, tourney_level) WHERE id = ?",
                        updates)
        self.conn.commit()
        return len(records)

    def backfill_extended_stats_surface(self):
        """Backfill surface and tourney_level for all extended stats tables
        by matching against the matches table using Python-side lookup."""
        tables = [
            "match_winners_errors", "match_serve_speed", "match_pbp_stats",
            "match_mcp_serve", "match_mcp_return", "match_mcp_rally",
            "match_mcp_tactics",
        ]
        # Build lookup from matches: (year, last_name) -> list of (tourney_name, surface, level)
        cur = self.conn.execute(
            "SELECT tourney_name, tourney_date, winner_name, loser_name, "
            "surface, tourney_level FROM matches")
        lookup = {}  # (year, last_name) -> [(tourney_name, surface, level)]
        for row in cur:
            tn, td, wn, ln, surf, lvl = row
            year = str(td)[:4] if td else ""
            for full_name in (wn, ln):
                if not full_name:
                    continue
                last = full_name.rsplit(" ", 1)[-1] if " " in full_name else full_name
                key = (year, last)
                if key not in lookup:
                    lookup[key] = []
                lookup[key].append((tn or "", surf, lvl))

        for table in tables:
            # Fetch rows needing backfill
            rows = self.conn.execute(
                f"SELECT id, tourney_name, tourney_date, opponent_name "
                f"FROM {table} "
                f"WHERE surface IS NULL OR surface = '' "
                f"   OR tourney_level IS NULL OR tourney_level = ''"
            ).fetchall()
            if not rows:
                continue
            updates = []
            for rid, etn, etd, opp in rows:
                year = str(etd)[:4] if etd else ""
                candidates = lookup.get((year, opp), [])
                surf = None
                lvl = None
                for mtn, ms, ml in candidates:
                    if etn and etn in mtn:
                        surf, lvl = ms, ml
                        break
                if surf is None:
                    # Fallback: try partial match
                    for mtn, ms, ml in candidates:
                        if etn and mtn and (etn in mtn or mtn in etn):
                            surf, lvl = ms, ml
                            break
                if surf or lvl:
                    updates.append((surf, lvl, rid))
            if updates:
                self.conn.executemany(
                    f"UPDATE {table} SET surface = COALESCE(?, surface), "
                    f"tourney_level = COALESCE(?, tourney_level) WHERE id = ?",
                    updates)
        self.conn.commit()

    def is_extended_cache_valid(self, player_name, expire_hours=168):
        """Check if extended stats cache is fresh (default: 1 week)."""
        row = self.conn.execute(
            "SELECT last_scraped FROM extended_stats_cache WHERE player_name = ?",
            (player_name,)).fetchone()
        if not row:
            return False
        from datetime import datetime, timedelta
        try:
            last = datetime.fromisoformat(row[0])
            return datetime.now() - last < timedelta(hours=expire_hours)
        except (ValueError, TypeError):
            return False

    @_locked_write
    def update_extended_stats_cache(self, player_name, tables_scraped,
                                    activity_fingerprint=None):
        """Record that extended stats were scraped for a player.

        If *activity_fingerprint* is None, the existing stored
        fingerprint is preserved.  See :meth:`update_scrape_cache`.
        """
        from datetime import datetime
        now_iso = datetime.now().isoformat()
        tables_csv = ",".join(tables_scraped)
        if activity_fingerprint is None:
            self.conn.execute(
                "INSERT INTO extended_stats_cache "
                "(player_name, last_scraped, tables_scraped, "
                "activity_fingerprint) "
                "VALUES (?, ?, ?, NULL) "
                "ON CONFLICT(player_name) DO UPDATE SET "
                "last_scraped = excluded.last_scraped, "
                "tables_scraped = excluded.tables_scraped",
                (player_name, now_iso, tables_csv))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO extended_stats_cache "
                "(player_name, last_scraped, tables_scraped, "
                "activity_fingerprint) "
                "VALUES (?, ?, ?, ?)",
                (player_name, now_iso, tables_csv, activity_fingerprint))
        self.conn.commit()

    def has_extended_new_activity(self, player_name, new_fingerprint):
        """Return True if extended stats should be re-scraped based on
        activity fingerprint change."""
        row = self.conn.execute(
            "SELECT activity_fingerprint FROM extended_stats_cache "
            "WHERE player_name = ?",
            (player_name,)).fetchone()
        if not row or row[0] is None:
            return True
        old_fp = row[0]
        if old_fp == new_fingerprint:
            return False
        old_combined = old_fp.replace("|", "")
        new_cur, new_prev = (new_fingerprint.split("|", 1) + [""])[:2]
        cur_is_new = bool(new_cur) and new_cur not in old_combined
        prev_is_new = bool(new_prev) and new_prev not in old_combined
        return cur_is_new or prev_is_new

    def get_all_extended_stats_cache(self):
        """Bulk-load the entire extended_stats_cache table.

        Returns a dict ``{player_name: (last_scraped_iso, fingerprint)}``.
        Used to avoid 1000+ sequential round-trips when checking activity
        for top-N rankings against a remote (Turso) database.
        """
        rows = self.conn.execute(
            "SELECT player_name, last_scraped, activity_fingerprint "
            "FROM extended_stats_cache"
        ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def close(self):
        self.conn.close()
