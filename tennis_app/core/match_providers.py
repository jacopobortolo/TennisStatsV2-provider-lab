"""Match-level providers for recent tennis results.

This lab module isolates the source of live match rows from the rest of
the app. Providers return the same DataFrame shape expected by
``TennisDatabase.import_scraped_matches``; extended stats remain handled by
the TennisAbstract ``player-more.cgi`` pipeline.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests

from .scraper import (
    LEVEL_MAP,
    TennisAbstractScraper,
    clean_player_name,
    convert_scraped_to_db_format,
)

logger = logging.getLogger(__name__)


MATCH_PROVIDER_ENV = "MATCH_PROVIDER"
SOFASCORE_API_BASE_ENV = "SOFASCORE_API_BASE_URL"
SOFASCORE_EVENT_PAGES_ENV = "SOFASCORE_EVENT_PAGES"
SOFASCORE_PLAYER_ID_CACHE_ENV = "SOFASCORE_PLAYER_ID_CACHE"
SOFASCORE_REQUEST_SLEEP_MAX_ENV = "SOFASCORE_REQUEST_SLEEP_MAX"
SOFASCORE_403_BREAKER_ENV = "SOFASCORE_403_BREAKER"


class SofaScoreAccessBlocked(RuntimeError):
    """Raised when SofaScore repeatedly returns HTTP 403."""


@dataclass
class ProviderFetchResult:
    """Normalized provider output before match-signature calculation."""

    df: pd.DataFrame
    last_match_date: str | None = None
    provider: str = "unknown"


class MatchProvider:
    """Base class for live match providers."""

    name = "base"

    def fetch_player_matches(
            self, player_name: str, min_year: int | None = None,
            tour: str = "atp", max_matches: int | None = None,
            existing_match_keys: set | None = None,
            skip_existing_matches: bool = False,
    ) -> ProviderFetchResult:
        raise NotImplementedError


class TennisAbstractMatchProvider(MatchProvider):
    """Current production provider, wrapped behind the common interface."""

    name = "tennisabstract"

    def __init__(self):
        self.scraper = TennisAbstractScraper()

    def fetch_player_matches(
            self, player_name: str, min_year: int | None = None,
            tour: str = "atp", max_matches: int | None = None,
            existing_match_keys: set | None = None,
            skip_existing_matches: bool = False,
    ) -> ProviderFetchResult:
        raw = self.scraper.fetch_player_matches(player_name, tour=tour)
        if raw is None:
            return ProviderFetchResult(pd.DataFrame(), None, self.name)

        last_match_date = None
        for match in raw:
            if match and len(match) > 0 and match[0] is not None:
                try:
                    date_text = str(int(match[0]))
                except (ValueError, TypeError):
                    continue
                if last_match_date is None or date_text > last_match_date:
                    last_match_date = date_text

        df = convert_scraped_to_db_format(
            raw, player_name, min_year=min_year,
            max_matches=max_matches, tour=tour,
        )
        return ProviderFetchResult(df, last_match_date, self.name)


class SofaScoreMatchProvider(MatchProvider):
    """Experimental SofaScore provider.

    The public web API is unofficial and may change. This provider starts
    with conservative, best-effort row construction and is intended for the
    lab copy only until field coverage is proven.
    """

    name = "sofascore"
    base_url = "https://www.sofascore.com/api/v1"

    _ROUND_MAP = {
        "final": "F",
        "finals": "F",
        "semifinal": "SF",
        "semifinals": "SF",
        "semi-final": "SF",
        "semi-finals": "SF",
        "quarterfinal": "QF",
        "quarterfinals": "QF",
        "quarter-final": "QF",
        "quarter-finals": "QF",
        "round of 16": "R16",
        "round of 32": "R32",
        "round of 64": "R64",
        "round of 128": "R128",
        "qualification final": "Q2",
        "qualifying final": "Q2",
        "final qualifying round": "Q2",
        "qualification round 2": "Q2",
        "qualifying round 2": "Q2",
        "qualification second round": "Q2",
        "qualifying second round": "Q2",
        "qualification round 1": "Q1",
        "qualifying round 1": "Q1",
        "qualification first round": "Q1",
        "qualifying first round": "Q1",
        "qualifications": "Q1",
        "qualification": "Q1",
        "qualifying": "Q1",
    }
    _SLAMS = {
        "australian open", "roland garros", "french open",
        "wimbledon", "us open",
    }
    _MASTERS_NAMES = {
        "indian wells", "miami", "monte carlo", "madrid", "rome",
        "italian open", "canadian open", "toronto", "montreal",
        "cincinnati", "shanghai", "paris masters",
    }
    _MASTERS_ALIAS_RULES = [
        ("monte carlo", "Monte Carlo", "Monte Carlo Masters", "Monte Carlo"),
        ("indian wells", "Indian Wells", "Indian Wells Masters", "Indian Wells"),
        ("miami", "Miami", "Miami Masters", "Miami"),
        ("madrid", "Madrid", "Madrid Masters", "WTA Madrid"),
        ("rome", "Rome", "Rome Masters", "WTA Rome"),
        ("italian open", "Rome", "Rome Masters", "WTA Rome"),
        ("cincinnati", "Cincinnati", "Cincinnati Masters", "Cincinnati"),
        ("shanghai", "Shanghai", "Shanghai Masters", "Shanghai"),
        ("paris masters", "Paris", "Paris Masters", "Paris"),
    ]
    _CLAY_EVENTS = {
        "monte carlo", "barcelona", "munich", "madrid", "rome",
        "italian open", "roland garros", "french open", "hamburg",
        "geneva", "lyon", "bastad", "gstaad", "kitzbuhel", "umag",
        "bucharest", "marrakech", "rio", "buenos aires", "santiago",
        "strasbourg", "palermo", "lausanne",
    }
    _GRASS_EVENTS = {
        "wimbledon", "halle", "queens", "queen's", "stuttgart open",
        "s-hertogenbosch", "hertogenbosch", "nottingham", "eastbourne",
        "mallorca", "berlin", "bad homburg", "birmingham",
    }
    _HARD_EVENTS = {
        "australian open", "us open", "indian wells", "miami",
        "canadian open", "toronto", "montreal", "cincinnati",
        "shanghai", "paris masters", "beijing", "tokyo", "doha",
        "dubai", "riyadh", "brisbane", "adelaide", "auckland",
        "washington", "acapulco", "rotterdam", "basel", "vienna",
        "united cup", "delray beach",
    }

    def __init__(self, timeout: int = 20, base_url: str | None = None):
        self.timeout = timeout
        self.base_url = (base_url or os.getenv(SOFASCORE_API_BASE_ENV)
                         or self.base_url).rstrip("/")
        try:
            self.event_pages = max(1, int(os.getenv(SOFASCORE_EVENT_PAGES_ENV, "3")))
        except ValueError:
            self.event_pages = 3
        try:
            self.request_sleep_max = max(
                0.0, float(os.getenv(SOFASCORE_REQUEST_SLEEP_MAX_ENV, "0")))
        except ValueError:
            self.request_sleep_max = 0.0
        try:
            self.breaker_threshold = max(
                1, int(os.getenv(SOFASCORE_403_BREAKER_ENV, "5")))
        except ValueError:
            self.breaker_threshold = 5
        self._consecutive_403 = 0
        self._player_id_cache = self._load_player_id_cache()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Origin": "https://www.sofascore.com",
            "Referer": "https://www.sofascore.com/",
        })

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_pause()
        url = f"{self.base_url}{path}"
        response = self.session.get(url, params=params, timeout=self.timeout)
        if response.status_code == 403:
            self._consecutive_403 += 1
            if self._consecutive_403 >= self.breaker_threshold:
                raise SofaScoreAccessBlocked(
                    "SofaScore returned HTTP 403 "
                    f"{self._consecutive_403} times consecutively; "
                    "stopping to avoid extending the block."
                )
        elif response.status_code < 400:
            self._consecutive_403 = 0
        response.raise_for_status()
        return response.json()

    def _request_pause(self):
        if self.request_sleep_max <= 0:
            return
        time.sleep(random.uniform(0.0, self.request_sleep_max))

    @staticmethod
    def _default_player_id_cache_path() -> Path:
        return Path.home() / ".tennis_analytics" / "data" / "sofascore_player_ids.json"

    def _player_id_cache_path(self) -> Path:
        raw = os.getenv(SOFASCORE_PLAYER_ID_CACHE_ENV)
        return Path(raw) if raw else self._default_player_id_cache_path()

    def _load_player_id_cache(self) -> dict[str, dict[str, Any]]:
        path = self._player_id_cache_path()
        try:
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
        except Exception as exc:
            logger.info("Could not read SofaScore player ID cache %s: %s", path, exc)
        return {}

    def _save_player_id_cache(self):
        path = self._player_id_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._player_id_cache, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:
            logger.info("Could not write SofaScore player ID cache %s: %s", path, exc)

    @classmethod
    def _player_cache_key(cls, player_name: str, tour: str) -> str:
        return f"{str(tour or '').lower()}:{cls._player_key_without_suffix(player_name)}"

    def _cached_player(self, player_name: str, tour: str) -> dict[str, Any] | None:
        item = self._player_id_cache.get(self._player_cache_key(player_name, tour))
        if not isinstance(item, dict) or not item.get("id"):
            return None
        return {
            "id": item.get("id"),
            "name": item.get("name") or player_name,
            "shortName": item.get("shortName") or item.get("name") or player_name,
            "sport": {"name": "Tennis"},
        }

    def _remember_player(self, player_name: str, tour: str, entity: dict[str, Any]):
        entity_id = entity.get("id")
        if not entity_id:
            return
        key = self._player_cache_key(player_name, tour)
        self._player_id_cache[key] = {
            "id": entity_id,
            "name": entity.get("name") or entity.get("shortName") or player_name,
            "shortName": entity.get("shortName") or entity.get("name") or player_name,
        }
        self._save_player_id_cache()

    @staticmethod
    def _norm(text: str | None) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip().lower()

    @staticmethod
    def _label_has(label: str, phrase: str) -> bool:
        label_key = re.sub(r"[^a-z0-9]+", " ", str(label or "").lower()).strip()
        phrase_key = re.sub(r"[^a-z0-9]+", " ", str(phrase or "").lower()).strip()
        return bool(phrase_key and f" {phrase_key} " in f" {label_key} ")

    @classmethod
    def _label_has_any(cls, label: str, phrases) -> bool:
        return any(cls._label_has(label, phrase) for phrase in phrases)

    @staticmethod
    def _strip_challenger_suffix(name: str) -> str:
        return re.sub(r"\s+challenger\s*$", "", str(name or "").strip(),
                      flags=re.IGNORECASE)

    @staticmethod
    def _player_key(text: str | None) -> str:
        if not text:
            return ""
        clean = clean_player_name(str(text)) or str(text)
        return re.sub(r"\s+", " ", clean).strip().lower()

    @classmethod
    def _player_key_without_suffix(cls, text: str | None) -> str:
        key = cls._player_key(text)
        return re.sub(r"\s+(?:jr\.?|sr\.?|ii|iii|iv)$", "", key).strip()

    @classmethod
    def _candidate_matches_player(cls, candidate_name: str, wanted: str) -> bool:
        if " - " in candidate_name:
            return False
        candidate = cls._player_key(candidate_name)
        candidate_base = cls._player_key_without_suffix(candidate_name)
        wanted_base = cls._player_key_without_suffix(wanted)
        return candidate == wanted_base or candidate_base == wanted_base

    @classmethod
    def _search_queries(cls, player_name: str) -> list[str]:
        queries = []
        for query in (player_name, clean_player_name(player_name)):
            query = re.sub(r"\s+", " ", str(query or "")).strip()
            if query and query not in queries:
                queries.append(query)
        return queries

    def search_players(self, player_name: str, tour: str = "atp",
                       use_cache: bool = True) -> list[dict[str, Any]]:
        """Return candidate SofaScore tennis entities for a player name."""
        cached = self._cached_player(player_name, tour) if use_cache else None
        if cached:
            return [cached]
        candidates: list[dict[str, Any]] = []
        seen_ids = set()
        for search_query in self._search_queries(player_name):
            query = quote(search_query)
            endpoints = (
                f"/search/all?q={query}&page=0",
                f"/search/teams?q={query}&page=0",
            )
            for endpoint in endpoints:
                try:
                    payload = self._get_json(endpoint)
                except SofaScoreAccessBlocked:
                    raise
                except Exception as exc:
                    logger.info("SofaScore search endpoint failed for %s: %s", endpoint, exc)
                    continue
                for item in payload.get("results") or payload.get("teams") or []:
                    entity = item.get("entity") if isinstance(item, dict) else item
                    if not isinstance(entity, dict):
                        continue
                    sport = self._norm((entity.get("sport") or {}).get("name"))
                    if sport and sport != "tennis":
                        continue
                    name = entity.get("name") or entity.get("shortName") or ""
                    if not name:
                        continue
                    short_name = entity.get("shortName") or name
                    if (not self._candidate_matches_player(name, player_name)
                            and not self._candidate_matches_player(short_name, player_name)):
                        continue
                    entity_id = entity.get("id")
                    dedupe_key = entity_id if entity_id is not None else name
                    if dedupe_key in seen_ids:
                        continue
                    seen_ids.add(dedupe_key)
                    candidates.append(entity)
        wanted = self._player_key(player_name)
        wanted_base = self._player_key_without_suffix(player_name)
        candidates.sort(key=lambda item: (
            self._player_key(item.get("name") or item.get("shortName")) != wanted,
            self._player_key_without_suffix(
                item.get("name") or item.get("shortName")) != wanted_base,
            item.get("name") or "",
        ))
        return candidates

    def fetch_player_events(self, player_name: str, tour: str = "atp",
                            pages: int = 1) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        candidates = self.search_players(player_name, tour=tour)
        if not candidates:
            return None, []
        last_player = candidates[0]
        for player in candidates:
            last_player = player
            player_id = player.get("id")
            if not player_id:
                continue
            events: list[dict[str, Any]] = []
            for page in range(max(1, pages)):
                try:
                    payload = self._get_json(f"/team/{player_id}/events/last/{page}")
                except SofaScoreAccessBlocked:
                    raise
                except Exception as exc:
                    logger.info(
                        "SofaScore events failed for %s candidate %s page %s: %s",
                        player_name, player.get("name") or player_id, page, exc)
                    events = []
                    break
                events.extend(payload.get("events") or [])
            if events:
                self._remember_player(player_name, tour, player)
                return player, events
        return last_player, []

    def fetch_player_matches(
            self, player_name: str, min_year: int | None = None,
            tour: str = "atp", max_matches: int | None = None,
            existing_match_keys: set | None = None,
            skip_existing_matches: bool = False,
    ) -> ProviderFetchResult:
        _, events = self.fetch_player_events(
            player_name, tour=tour, pages=self.event_pages)
        existing_match_keys = existing_match_keys or set()
        rows = []
        last_match_date = None
        for event in events:
            row = self._event_to_match_row(event, player_name, tour=tour)
            if not row:
                continue
            if self._match_key_from_row(row) in existing_match_keys:
                if skip_existing_matches:
                    continue
                row["_skip_sofascore_statistics"] = True
            date_text = row["tourney_date"]
            if min_year and date_text < f"{int(min_year)}0000":
                continue
            if last_match_date is None or date_text > last_match_date:
                last_match_date = date_text
            rows.append((row, event))

        if not rows:
            return ProviderFetchResult(pd.DataFrame(), last_match_date, self.name)
        rows.sort(key=lambda item: item[0]["tourney_date"], reverse=True)
        if max_matches is not None and max_matches > 0:
            rows = rows[:max_matches]
        for row, event in rows:
            if row.pop("_skip_sofascore_statistics", False):
                continue
            row.update(self._event_statistics_to_match_stats(event))
        df = pd.DataFrame([row for row, _event in rows])
        return ProviderFetchResult(df, last_match_date, self.name)

    @classmethod
    def _match_key_from_row(cls, row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
        return cls.match_key(
            row.get("tour"), row.get("tourney_date"), row.get("winner_name"),
            row.get("loser_name"), row.get("tourney_name"), row.get("round"),
        )

    @classmethod
    def match_key(cls, tour, tourney_date, winner_name, loser_name,
                  tourney_name, round_name) -> tuple[str, str, str, str, str, str]:
        return (
            str(tour or "").strip().lower(),
            str(tourney_date or "").strip(),
            cls._player_key(winner_name),
            cls._player_key(loser_name),
            cls._norm(str(tourney_name or "")),
            str(round_name or "").strip().upper(),
        )

    def _event_to_match_row(self, event: dict[str, Any], player_name: str,
                            tour: str = "atp") -> dict[str, Any] | None:
        status_type = self._norm((event.get("status") or {}).get("type"))
        if status_type not in {"finished", "afterpenalties", "ended"}:
            return None
        home = event.get("homeTeam") or {}
        away = event.get("awayTeam") or {}
        winner_code = event.get("winnerCode")
        if winner_code not in (1, 2):
            return None

        winner = home if winner_code == 1 else away
        loser = away if winner_code == 1 else home
        winner_name = self._clean_team_name(
            winner.get("name") or winner.get("shortName"))
        loser_name = self._clean_team_name(
            loser.get("name") or loser.get("shortName"))
        if not winner_name or not loser_name:
            return None

        start_ts = event.get("startTimestamp")
        if start_ts:
            date_text = _dt.datetime.utcfromtimestamp(int(start_ts)).strftime("%Y%m%d")
        else:
            date_text = ""
        if not date_text:
            return None

        tournament = event.get("tournament") or {}
        unique_tournament = event.get("uniqueTournament") or {}
        tourney_name = (
            unique_tournament.get("name") or tournament.get("name")
            or event.get("slug") or ""
        )
        if self._is_doubles_event(event, home, away, tourney_name):
            return None
        tourney_name = self._canonical_tourney_name(tourney_name, event, tour)

        return {
            "tourney_id": "",
            "tourney_name": tourney_name,
            "surface": self._surface_from_event(event, tour=tour),
            "draw_size": None,
            "tourney_level": self._level_from_event(event, tour=tour),
            "tourney_date": date_text,
            "source_match_date": date_text,
            "match_num": None,
            "winner_id": "",
            "winner_seed": self._seed_from_team(winner),
            "winner_entry": None,
            "winner_name": winner_name,
            "winner_hand": None,
            "winner_ht": None,
            "winner_ioc": self._ioc_from_team(winner),
            "winner_age": None,
            "loser_id": "",
            "loser_seed": self._seed_from_team(loser),
            "loser_entry": None,
            "loser_name": loser_name,
            "loser_hand": None,
            "loser_ht": None,
            "loser_ioc": self._ioc_from_team(loser),
            "loser_age": None,
            "score": self._score_from_event(event, winner_code),
            "best_of": None,
            "round": self._round_from_event(event),
            "minutes": self._minutes_from_event(event),
            "w_ace": None,
            "w_df": None,
            "w_svpt": None,
            "w_1stIn": None,
            "w_1stWon": None,
            "w_2ndWon": None,
            "w_SvGms": None,
            "w_bpSaved": None,
            "w_bpFaced": None,
            "l_ace": None,
            "l_df": None,
            "l_svpt": None,
            "l_1stIn": None,
            "l_1stWon": None,
            "l_2ndWon": None,
            "l_SvGms": None,
            "l_bpSaved": None,
            "l_bpFaced": None,
            "winner_rank": None,
            "winner_rank_points": None,
            "loser_rank": None,
            "loser_rank_points": None,
            "tour": tour,
            "is_upcoming": 0,
        }

    def _event_statistics_to_match_stats(self, event: dict[str, Any]) -> dict[str, Any]:
        event_id = event.get("id")
        winner_code = event.get("winnerCode")
        if not event_id or winner_code not in (1, 2):
            return {}
        try:
            payload = self._get_json(f"/event/{event_id}/statistics")
        except SofaScoreAccessBlocked:
            raise
        except Exception as exc:
            logger.info("SofaScore statistics failed for event %s: %s", event_id, exc)
            return {}
        raw_stats = self._home_away_statistics(payload)
        if not raw_stats:
            return {}

        winner_side = "home" if winner_code == 1 else "away"
        loser_side = "away" if winner_code == 1 else "home"
        return {
            "w_ace": self._stat_value(raw_stats, "aces", winner_side),
            "w_df": self._stat_value(raw_stats, "doubleFaults", winner_side),
            "w_svpt": self._stat_total(raw_stats, "firstServeAccuracy", winner_side),
            "w_1stIn": self._stat_value(raw_stats, "firstServeAccuracy", winner_side),
            "w_1stWon": self._stat_value(raw_stats, "firstServePointsAccuracy", winner_side),
            "w_2ndWon": self._stat_value(raw_stats, "secondServePointsAccuracy", winner_side),
            "w_SvGms": self._stat_value(raw_stats, "serviceGamesTotal", winner_side),
            "w_bpSaved": self._stat_value(raw_stats, "breakPointsSaved", winner_side),
            "w_bpFaced": self._stat_total(raw_stats, "breakPointsSaved", winner_side),
            "l_ace": self._stat_value(raw_stats, "aces", loser_side),
            "l_df": self._stat_value(raw_stats, "doubleFaults", loser_side),
            "l_svpt": self._stat_total(raw_stats, "firstServeAccuracy", loser_side),
            "l_1stIn": self._stat_value(raw_stats, "firstServeAccuracy", loser_side),
            "l_1stWon": self._stat_value(raw_stats, "firstServePointsAccuracy", loser_side),
            "l_2ndWon": self._stat_value(raw_stats, "secondServePointsAccuracy", loser_side),
            "l_SvGms": self._stat_value(raw_stats, "serviceGamesTotal", loser_side),
            "l_bpSaved": self._stat_value(raw_stats, "breakPointsSaved", loser_side),
            "l_bpFaced": self._stat_total(raw_stats, "breakPointsSaved", loser_side),
        }

    @classmethod
    def _home_away_statistics(cls, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for period in payload.get("statistics") or []:
            if cls._norm(period.get("period")) != "all":
                continue
            for group in period.get("groups") or []:
                if cls._norm(group.get("groupName")) != "service":
                    continue
                for item in group.get("statisticsItems") or []:
                    key = item.get("key")
                    if key:
                        stats[str(key)] = item
            break
        return stats

    @classmethod
    def _stat_value(cls, stats: dict[str, dict[str, Any]], key: str,
                    side: str) -> float | None:
        return cls._stat_number(stats, key, f"{side}Value", side)

    @classmethod
    def _stat_total(cls, stats: dict[str, dict[str, Any]], key: str,
                    side: str) -> float | None:
        return cls._stat_number(stats, key, f"{side}Total")

    @staticmethod
    def _stat_number(stats: dict[str, dict[str, Any]], key: str,
                     *fields: str) -> float | None:
        item = stats.get(key) or {}
        for field in fields:
            value = item.get(field)
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _score_from_event(self, event: dict[str, Any], winner_code: int) -> str:
        home_score = event.get("homeScore") or {}
        away_score = event.get("awayScore") or {}
        tokens = []
        for idx in range(1, 6):
            home_games = home_score.get(f"period{idx}")
            away_games = away_score.get(f"period{idx}")
            if home_games is None or away_games is None:
                continue
            if winner_code == 1:
                tokens.append(f"{int(home_games)}-{int(away_games)}")
            else:
                tokens.append(f"{int(away_games)}-{int(home_games)}")
        return " ".join(tokens)

    def _round_from_event(self, event: dict[str, Any]) -> str:
        round_info = event.get("roundInfo") or {}
        raw = round_info.get("name") or round_info.get("round") or ""
        text = self._norm(str(raw))
        label = self._tournament_label(event)
        is_qualifying_event = self._label_has_any(
            label, ("qualifying", "qualification"))
        if "qual" in text or is_qualifying_event:
            combined = f"{text} {label}"
            if "final" in combined or re.search(r"\b(?:2|2nd|second)\b", combined):
                return "Q2"
            return "Q1"
        if text in self._ROUND_MAP:
            return self._ROUND_MAP[text]
        match = re.search(r"(\d+)", text)
        if "round" in text and match:
            return self._provider_round_from_ordinal(int(match.group(1)))
        if self._label_has_any(label, ("united cup", "davis cup", "billie jean")):
            return "RR"
        return str(raw or "")

    @staticmethod
    def _provider_round_from_ordinal(ordinal: int) -> str:
        if ordinal == 1:
            return "Q1"
        if ordinal == 2:
            return "Q2"
        return f"R{ordinal}"

    @staticmethod
    def _minutes_from_event(event: dict[str, Any]) -> int | None:
        time_info = event.get("time") or {}
        seconds = 0
        for key, value in time_info.items():
            if not str(key).startswith("period"):
                continue
            try:
                seconds += int(value)
            except (TypeError, ValueError):
                continue
        return round(seconds / 60) if seconds > 0 else None

    @staticmethod
    def _clean_team_name(name: str | None) -> str | None:
        if not name:
            return name
        return re.sub(
            r"\s*\((?:\d+|PR|Q|WC|LL|SE|ALT)\)\s*$",
            "",
            str(name).strip(),
            flags=re.IGNORECASE,
        )

    def _is_doubles_event(self, event: dict[str, Any], home: dict[str, Any],
                          away: dict[str, Any], tourney_name: str) -> bool:
        label = self._tournament_label(event)
        if self._label_has(label, "doubles") or self._label_has(tourney_name, "doubles"):
            return True
        names = [
            str(home.get("name") or home.get("shortName") or ""),
            str(away.get("name") or away.get("shortName") or ""),
        ]
        return any("/" in name for name in names)

    def _canonical_tourney_name(self, name: str, event: dict[str, Any],
                                tour: str = "atp") -> str:
        label = self._tournament_label(event)
        if self._label_has(label, "united cup"):
            return "United Cup"
        if self._label_has(label, "delray beach"):
            return "Delray Beach"
        for pattern, city, atp_name, wta_name in self._MASTERS_ALIAS_RULES:
            if self._label_has(label, pattern):
                level = self._level_from_event(event, tour=tour)
                if self._label_has(label, "challenger") or level == "C":
                    return f"{city} CH"
                if str(level).isdigit():
                    return f"{'W' if tour == 'wta' else 'M'}{level} {city}"
                if level == "M":
                    return atp_name
                if level in {"PM", "P", "W"}:
                    return wta_name
                return wta_name if tour == "wta" else city
        if self._label_has(label, "acapulco"):
            return "Acapulco"
        if self._label_has(label, "munich"):
            return "Munich"
        if self._label_has(label, "stuttgart"):
            return "Stuttgart"
        if ", " in name:
            name = name.split(", ", 1)[0]
        level = self._level_from_event(event, tour=tour)
        if level == "C" and str(tour).lower() == "atp":
            name = self._strip_challenger_suffix(name)
        if level == "C" and str(tour).lower() == "atp" and not self._label_has(name, "CH"):
            return f"{name} CH"
        return name

    def _surface_from_event(self, event: dict[str, Any], tour: str = "atp") -> str:
        for key in ("groundType", "courtType", "surface"):
            raw = event.get(key)
            if isinstance(raw, dict):
                raw = raw.get("name") or raw.get("slug")
            text = self._norm(str(raw or ""))
            if "clay" in text:
                return "Clay"
            if "grass" in text:
                return "Grass"
            if "hard" in text:
                return "Hard"
            if "carpet" in text:
                return "Carpet"
        label = self._tournament_label(event)
        if self._label_has(label, "stuttgart"):
            return "Clay" if tour == "wta" else "Grass"
        if self._label_has_any(label, self._CLAY_EVENTS):
            return "Clay"
        if self._label_has_any(label, self._GRASS_EVENTS):
            return "Grass"
        if self._label_has_any(label, self._HARD_EVENTS):
            return "Hard"
        return ""

    def _level_from_event(self, event: dict[str, Any], tour: str = "atp") -> str:
        label = self._tournament_label(event)
        if self._label_has_any(label, self._SLAMS):
            return "G"
        if self._label_has(label, "challenger"):
            return "C"
        itf_match = re.search(r"\b[wm](15|25|35|50|60|75|80|100|125)\b", label)
        if itf_match:
            return itf_match.group(1)
        if (self._label_has(label, "masters") or self._label_has(label, "1000")
                or self._label_has_any(label, self._MASTERS_NAMES)):
            return "PM" if tour == "wta" else "M"
        if self._label_has(label, "finals"):
            return "F"
        if self._label_has(label, "davis") or self._label_has(label, "billie jean"):
            return "D"
        if self._label_has(label, "olympic"):
            return "O"
        return LEVEL_MAP.get("A", "A") if tour == "atp" else "I"

    def _event_label(self, event: dict[str, Any]) -> str:
        label_parts = self._tournament_label_parts(event)
        label_parts.append(str(event.get("slug") or ""))
        return self._norm(" ".join(label_parts))

    def _tournament_label(self, event: dict[str, Any]) -> str:
        return self._norm(" ".join(self._tournament_label_parts(event)))

    @staticmethod
    def _tournament_label_parts(event: dict[str, Any]) -> list[str]:
        tournament = event.get("tournament") or {}
        unique_tournament = event.get("uniqueTournament") or {}
        nested_unique = tournament.get("uniqueTournament") or {}
        return [
            str(tournament.get("name") or ""),
            str(unique_tournament.get("name") or ""),
            str(nested_unique.get("name") or ""),
            str((tournament.get("category") or {}).get("name") or ""),
        ]

    @staticmethod
    def _rank_from_team(team: dict[str, Any]) -> float | None:
        for key in ("ranking", "rank", "currentRanking"):
            value = team.get(key)
            if isinstance(value, dict):
                value = value.get("rank") or value.get("position")
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _seed_from_team(team: dict[str, Any]) -> str | None:
        value = team.get("seed") or team.get("seeding")
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _ioc_from_team(team: dict[str, Any]) -> str | None:
        country = team.get("country") or {}
        value = country.get("alpha3") or country.get("alpha2") or country.get("name")
        return str(value).upper() if value else None


class HybridMatchProvider(MatchProvider):
    """Try a fast provider first, then fall back to TennisAbstract."""

    name = "hybrid"

    def __init__(self):
        self.fast_provider = SofaScoreMatchProvider()
        self.fallback_provider = TennisAbstractMatchProvider()

    def fetch_player_matches(
            self, player_name: str, min_year: int | None = None,
            tour: str = "atp", max_matches: int | None = None,
            existing_match_keys: set | None = None,
            skip_existing_matches: bool = False,
    ) -> ProviderFetchResult:
        try:
            result = self.fast_provider.fetch_player_matches(
                player_name, min_year=min_year, tour=tour,
                max_matches=max_matches,
                existing_match_keys=existing_match_keys,
                skip_existing_matches=skip_existing_matches,
            )
            if result.df is not None and not result.df.empty:
                result.provider = self.name + ":" + self.fast_provider.name
                return result
        except SofaScoreAccessBlocked:
            raise
        except Exception as exc:
            logger.info("Fast match provider failed for %s: %s", player_name, exc)
        result = self.fallback_provider.fetch_player_matches(
            player_name, min_year=min_year, tour=tour,
            max_matches=max_matches,
            existing_match_keys=existing_match_keys,
            skip_existing_matches=skip_existing_matches,
        )
        result.provider = self.name + ":" + self.fallback_provider.name
        return result


_PROVIDER_CACHE: dict[str, MatchProvider] = {}


def get_match_provider(provider_name: str | None = None) -> MatchProvider:
    """Return the configured provider.

    ``MATCH_PROVIDER`` may be ``tennisabstract`` (default), ``sofascore`` or
    ``hybrid``. The lab copy uses this hook; production can keep the default.
    """
    selected = (provider_name or os.getenv(MATCH_PROVIDER_ENV)
                or "tennisabstract").strip().lower()
    aliases = {
        "ta": "tennisabstract",
        "tennis_abstract": "tennisabstract",
        "sf": "sofascore",
        "sofa": "sofascore",
    }
    selected = aliases.get(selected, selected)
    if selected not in _PROVIDER_CACHE:
        if selected == "tennisabstract":
            _PROVIDER_CACHE[selected] = TennisAbstractMatchProvider()
        elif selected == "sofascore":
            _PROVIDER_CACHE[selected] = SofaScoreMatchProvider()
        elif selected == "hybrid":
            _PROVIDER_CACHE[selected] = HybridMatchProvider()
        else:
            raise ValueError(
                f"Unknown match provider {provider_name!r}; expected "
                "tennisabstract, sofascore or hybrid"
            )
    return _PROVIDER_CACHE[selected]