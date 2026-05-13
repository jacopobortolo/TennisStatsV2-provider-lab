"""Match-level providers for recent tennis results.

This lab module isolates the source of live match rows from the rest of
the app. Providers return the same DataFrame shape expected by
``TennisDatabase.import_scraped_matches``; extended stats remain handled by
the TennisAbstract ``player-more.cgi`` pipeline.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
from dataclasses import dataclass
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
        url = f"{self.base_url}{path}"
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _norm(text: str | None) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip().lower()

    @staticmethod
    def _player_key(text: str | None) -> str:
        if not text:
            return ""
        clean = clean_player_name(str(text)) or str(text)
        return re.sub(r"\s+", " ", clean).strip().lower()

    @classmethod
    def _search_queries(cls, player_name: str) -> list[str]:
        queries = []
        for query in (player_name, clean_player_name(player_name)):
            query = re.sub(r"\s+", " ", str(query or "")).strip()
            if query and query not in queries:
                queries.append(query)
        return queries

    def search_players(self, player_name: str, tour: str = "atp") -> list[dict[str, Any]]:
        """Return candidate SofaScore tennis entities for a player name."""
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
                    entity_id = entity.get("id")
                    dedupe_key = entity_id if entity_id is not None else name
                    if dedupe_key in seen_ids:
                        continue
                    seen_ids.add(dedupe_key)
                    candidates.append(entity)
        wanted = self._player_key(player_name)
        candidates.sort(key=lambda item: (
            self._player_key(item.get("name") or item.get("shortName")) != wanted,
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
                except Exception as exc:
                    logger.info(
                        "SofaScore events failed for %s candidate %s page %s: %s",
                        player_name, player.get("name") or player_id, page, exc)
                    events = []
                    break
                events.extend(payload.get("events") or [])
            if events:
                return player, events
        return last_player, []

    def fetch_player_matches(
            self, player_name: str, min_year: int | None = None,
            tour: str = "atp", max_matches: int | None = None,
    ) -> ProviderFetchResult:
        _, events = self.fetch_player_events(
            player_name, tour=tour, pages=self.event_pages)
        rows = []
        last_match_date = None
        for event in events:
            row = self._event_to_match_row(event, player_name, tour=tour)
            if not row:
                continue
            date_text = row["tourney_date"]
            if min_year and date_text < f"{int(min_year)}0000":
                continue
            if last_match_date is None or date_text > last_match_date:
                last_match_date = date_text
            rows.append(row)

        if not rows:
            return ProviderFetchResult(pd.DataFrame(), last_match_date, self.name)
        df = pd.DataFrame(rows).sort_values("tourney_date", ascending=False)
        if max_matches is not None and max_matches > 0:
            df = df.head(max_matches).reset_index(drop=True)
        return ProviderFetchResult(df, last_match_date, self.name)

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
        if text in self._ROUND_MAP:
            return self._ROUND_MAP[text]
        if "qual" in text:
            if "final" in text or re.search(r"\b(?:2|2nd|second)\b", text):
                return "Q2"
            return "Q1"
        match = re.search(r"(\d+)", text)
        if "round" in text and match:
            return f"R{match.group(1)}"
        label = self._event_label(event)
        if "united cup" in label or "davis cup" in label or "billie jean" in label:
            return "RR"
        return str(raw or "")

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
        label = self._event_label(event)
        if "doubles" in label or " doubles" in self._norm(tourney_name):
            return True
        names = [
            str(home.get("name") or home.get("shortName") or ""),
            str(away.get("name") or away.get("shortName") or ""),
        ]
        return any("/" in name for name in names)

    def _canonical_tourney_name(self, name: str, event: dict[str, Any],
                                tour: str = "atp") -> str:
        label = self._event_label(event)
        if "united cup" in label:
            return "United Cup"
        if "delray beach" in label:
            return "Delray Beach"
        for pattern, city, atp_name, wta_name in self._MASTERS_ALIAS_RULES:
            if pattern in label:
                level = self._level_from_event(event, tour=tour)
                if "challenger" in label or level == "C":
                    return f"{city} CH"
                if str(level).isdigit():
                    return f"{'W' if tour == 'wta' else 'M'}{level} {city}"
                if level == "M":
                    return atp_name
                if level in {"PM", "P", "W"}:
                    return wta_name
                return wta_name if tour == "wta" else city
        if "acapulco" in label:
            return "Acapulco"
        if "munich" in label:
            return "Munich"
        if "stuttgart" in label:
            return "Stuttgart"
        if ", " in name:
            return name.split(", ", 1)[0]
        return name

    def _surface_from_event(self, event: dict[str, Any], tour: str = "atp") -> str:
        for key in ("groundType", "courtType", "surface"):
            raw = event.get(key)
            if isinstance(raw, dict):
                raw = raw.get("name")
            text = self._norm(str(raw or ""))
            if text in {"hard", "clay", "grass", "carpet"}:
                return text.capitalize()
        label = self._event_label(event)
        if "stuttgart" in label:
            return "Clay" if tour == "wta" else "Grass"
        if any(name in label for name in self._CLAY_EVENTS):
            return "Clay"
        if any(name in label for name in self._GRASS_EVENTS):
            return "Grass"
        if any(name in label for name in self._HARD_EVENTS):
            return "Hard"
        return ""

    def _level_from_event(self, event: dict[str, Any], tour: str = "atp") -> str:
        label = self._event_label(event)
        if any(slam in label for slam in self._SLAMS):
            return "G"
        if "challenger" in label:
            return "C"
        itf_match = re.search(r"\b[wm](15|25|35|50|60|75|80|100|125)\b", label)
        if itf_match:
            return itf_match.group(1)
        if "masters" in label or "1000" in label or any(
                name in label for name in self._MASTERS_NAMES):
            return "PM" if tour == "wta" else "M"
        if "finals" in label:
            return "F"
        if "davis" in label or "billie jean" in label:
            return "D"
        if "olympic" in label:
            return "O"
        return LEVEL_MAP.get("A", "A") if tour == "atp" else "I"

    def _event_label(self, event: dict[str, Any]) -> str:
        tournament = event.get("tournament") or {}
        unique_tournament = event.get("uniqueTournament") or {}
        return self._norm(" ".join([
            str(tournament.get("name") or ""),
            str(unique_tournament.get("name") or ""),
            str((tournament.get("category") or {}).get("name") or ""),
            str(event.get("slug") or ""),
        ]))

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
    ) -> ProviderFetchResult:
        try:
            result = self.fast_provider.fetch_player_matches(
                player_name, min_year=min_year, tour=tour,
                max_matches=max_matches,
            )
            if result.df is not None and not result.df.empty:
                result.provider = self.name + ":" + self.fast_provider.name
                return result
        except Exception as exc:
            logger.info("Fast match provider failed for %s: %s", player_name, exc)
        result = self.fallback_provider.fetch_player_matches(
            player_name, min_year=min_year, tour=tour,
            max_matches=max_matches,
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