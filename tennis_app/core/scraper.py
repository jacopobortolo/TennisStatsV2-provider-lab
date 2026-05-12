"""
Scraper for fetching live player match data from tennisabstract.com.

This provides access to up-to-date match data (2025/2026+) that is not
available in Jeff Sackmann's GitHub CSV files (frozen at 2024).
"""

import ast
import logging
import random
import re
import time
import unicodedata

import cloudscraper
import requests
import pandas as pd
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tennisabstract.com"
REQUEST_DELAY_MIN = 5.0
REQUEST_DELAY_MAX = 8.0
REQUEST_RETRIES = 3

# Column mapping from the JS array indices to field names
# (based on tennisabstract's matchmx array structure)
MATCH_COLUMNS = {
    0: "date",
    1: "tourn",
    2: "surf",
    3: "level",
    4: "wl",
    5: "prank",
    6: "pseed",
    7: "pentry",
    8: "round",
    9: "score",
    11: "opp",
    12: "orank",
    13: "oseed",
    14: "oentry",
    18: "oioc",
    20: "minutes",
    21: "aces",
    22: "dfs",
    23: "svpt",
    24: "first_in",
    25: "first_won",
    26: "second_won",
    27: "sv_gms",
    28: "bp_saved",
    29: "bp_faced",
    30: "o_aces",
    31: "o_dfs",
    32: "o_svpt",
    33: "o_first_in",
    34: "o_first_won",
    35: "o_second_won",
    36: "o_sv_gms",
    37: "o_bp_saved",
    38: "o_bp_faced",
}

# Sackmann CSV name → TennisAbstract URL name overrides.
# Used when the transliteration/spelling on TA differs from the Sackmann name.
# Key: exact Sackmann full name (as it appears in the players CSV).
# Value: TennisAbstract URL slug (CamelCase, no spaces).
TA_NAME_OVERRIDES = {
    # Sackmann uses "Ludmilla", TA uses "Liudmila"
    "Ludmilla Samsonova": "LiudmilaSamsonova",
}

# Map tennisabstract tourney level codes to Sackmann-style codes
LEVEL_MAP = {
    "G": "G",       # Grand Slam
    "M": "M",       # Masters 1000
    "A": "A",       # ATP 250/500
    "F": "F",       # Tour Finals
    "D": "D",       # Davis Cup
    "C": "C",       # Challenger
}


class TennisAbstractScraper:
    """Scrapes match data from tennisabstract.com."""

    def __init__(self):
        self.session = cloudscraper.create_scraper()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })
        self._delay_multiplier = 1.0  # adaptive: increases after 429s
        self._request_count = 0  # track requests to skip first delay

    def _jittered_delay(self, multiplier=1):
        """Sleep for a random duration between REQUEST_DELAY_MIN and REQUEST_DELAY_MAX."""
        delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX) * multiplier * self._delay_multiplier
        time.sleep(delay)

    def _make_request(self, url, raise_on_error=True):
        """Make HTTP request with retry logic and rate-limit handling."""
        for attempt in range(REQUEST_RETRIES):
            try:
                # Jittered delay: skip on very first request of the session
                if attempt > 0:
                    self._jittered_delay(multiplier=2 ** attempt)
                elif self._request_count > 0:
                    self._jittered_delay()

                self._request_count += 1

                resp = self.session.get(url, timeout=30)

                if resp.status_code == 429:
                    # Increase adaptive delay for future requests
                    self._delay_multiplier = min(self._delay_multiplier * 1.5, 3.0)
                    wait = 30
                    logger.warning("Rate limited (attempt %d), waiting %ds... "
                                   "(delay multiplier now %.1f)",
                                   attempt + 1, wait, self._delay_multiplier)
                    time.sleep(wait)
                    continue
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                # Successful request: slowly reduce adaptive delay
                if self._delay_multiplier > 1.0:
                    self._delay_multiplier = max(1.0, self._delay_multiplier * 0.95)
                return resp
            except requests.RequestException as exc:
                logger.warning("Attempt %d failed for %s: %s",
                               attempt + 1, url, exc)
                if attempt == REQUEST_RETRIES - 1:
                    if raise_on_error:
                        raise
                    return None
        return None

    # Characters that NFKD does not decompose — map to ASCII equivalents
    _EXTRA_TRANSLIT = str.maketrans({
        "ø": "o", "Ø": "O", "ł": "l", "Ł": "L",
        "đ": "d", "Đ": "D", "æ": "ae", "Æ": "AE",
        "œ": "oe", "Œ": "OE", "ß": "ss",
        "þ": "th", "Þ": "Th",
        # Turkish letters that NFKD doesn't fully decompose
        "ı": "i", "İ": "I", "ş": "s", "Ş": "S",
        "ğ": "g", "Ğ": "G",
        "'": None, "\u2019": None, "\u2018": None,  # apostrophes
    })

    @staticmethod
    def _clean_name(name):
        """Normalize whitespace, diacritics, and hyphens in a player name."""
        # Transliterate chars that NFKD cannot decompose & strip apostrophes
        name = name.translate(TennisAbstractScraper._EXTRA_TRANSLIT)
        # Decompose unicode (e.g. ć → c + combining accent)
        name = unicodedata.normalize("NFKD", name)
        # Strip combining marks (accents, diacritics)
        name = "".join(c for c in name if not unicodedata.combining(c))
        # Remove hyphens (e.g. Auger-Aliassime → Auger Aliassime)
        name = name.replace("-", " ")
        name = re.sub(r"\s+", " ", name).strip()
        return name

    def fetch_player_matches(self, player_name, tour="atp"):
        """
        Fetch all matches for a player from tennisabstract.com.

        Returns a list of raw match arrays, or None if not found.
        """
        original_name = player_name
        player_name = self._clean_name(player_name)
        # Title-case each word to match tennisabstract URL format
        # e.g. "Botic van de Zandschulp" → "BoticVanDeZandschulp"
        player_url_name = "".join(
            w.capitalize() for w in player_name.split() if w
        )

        # Manual overrides: known transliteration mismatches between
        # Sackmann CSV names and TennisAbstract URL slugs.
        if original_name in TA_NAME_OVERRIDES:
            player_url_name = TA_NAME_OVERRIDES[original_name]
            url_variants = [player_url_name]
        else:
            # Build alternative URL forms.  When the original name contained
            # hyphens (e.g. "Han-na Chang"), TA may store the player without
            # any space between the two parts ("HannaChang") rather than
            # with a capital after the hyphen ("HanNaChang").  Generate
            # every adjacent-pair-collapsed variant so we can probe both.
            url_variants = [player_url_name]
            if "-" in original_name:
                words = [w for w in player_name.split() if w]
                for i in range(len(words) - 1):
                    merged = (words[:i]
                              + [(words[i] + words[i + 1]).capitalize()]
                              + words[i + 2:])
                    variant = "".join(w.capitalize() for w in merged)
                    if variant and variant not in url_variants:
                        url_variants.append(variant)

        all_matches = []

        # ATP inlines matchmx in player-classic.cgi. WTA classic pages load
        # matchmx through a linked jsmatches script; still keep the same
        # page-first flow and only fall back to direct JS if the page fails.
        cgi = "wplayer-classic.cgi" if tour == "wta" else "player-classic.cgi"

        # Try HTML page first, then jsmatches/ JS fallback.
        def _try_one(url_name):
            url = f"{BASE_URL}/cgi-bin/{cgi}?p={url_name}"
            # Two HTML attempts before falling back to the static JS file.
            for attempt in (1, 2):
                try:
                    resp = self._make_request(url, raise_on_error=False)
                    if resp is None:
                        logger.warning(
                            "HTML returned None for %s (attempt %d/2)",
                            url_name, attempt)
                    elif "No player found" in resp.text:
                        # Genuine "missing player" — no point retrying.
                        logger.info(
                            "HTML 'No player found' for %s", url_name)
                        break
                    else:
                        matches = self._parse_matches_from_html(resp.text)
                        if matches:
                            logger.info(
                                "Found %d matches in HTML for %s",
                                len(matches), url_name)
                            return matches

                        matches, found_linked_js = self._fetch_matches_from_html_scripts(resp.text)
                        if matches:
                            logger.info(
                                "Found %d matches from HTML-linked JS for %s",
                                len(matches), url_name)
                            return matches
                        if found_linked_js:
                            logger.warning(
                                "HTML-linked JS for %s did not yield matches; "
                                "skipping duplicate HTML retry",
                                url_name)
                            break

                        logger.warning(
                            "HTML response for %s parsed to 0 matches "
                            "(attempt %d/2, html_len=%d)",
                            url_name, attempt, len(resp.text))
                except Exception as exc:
                    logger.warning("HTML fetch failed for %s (attempt %d/2): %s",
                                   url_name, attempt, exc)
                if attempt == 1:
                    # Short backoff between HTML retries.
                    time.sleep(random.uniform(2.0, 4.0))
            for js_url in (
                f"{BASE_URL}/jsmatches/{url_name}.js",
                f"{BASE_URL}/jsmatches/{url_name}Career.js",
            ):
                try:
                    resp = self._make_request(js_url, raise_on_error=False)
                    if resp and resp.status_code == 200:
                        matches = self._parse_matches_from_js(resp.text)
                        if matches:
                            try:
                                latest = max(
                                    int(m[0]) for m in matches
                                    if m and m[0] not in (None, "")
                                )
                                logger.info(
                                    "Found %d matches from JS fallback: %s "
                                    "(latest=%s)",
                                    len(matches), js_url, latest)
                            except (ValueError, TypeError):
                                logger.info(
                                    "Found %d matches from JS fallback: %s",
                                    len(matches), js_url)
                            return matches
                except Exception as exc:
                    logger.warning("Could not get JS matches from %s: %s",
                                   js_url, exc)
            return []

        for url_name in url_variants:
            found = _try_one(url_name)
            if found:
                all_matches.extend(found)
                # Use the variant that worked for any subsequent fallback
                player_url_name = url_name
                break

        if all_matches:
            return all_matches

        # Last-chance: resolve via tennisabstract's official playerlist.
        # Handles cases where the cleaned-name URL doesn't exist on TA
        # but the player is registered under a different spelling
        # (e.g. "Stephanie Visscher" \u2192 "Stephanie Judith Visscher",
        # "Han-na Chang" \u2192 "Hanna Chang").
        try:
            resolved = self._resolve_via_playerlist(player_name, tour=tour)
        except Exception as exc:
            logger.warning("Playerlist fallback failed for %s: %s",
                           player_name, exc)
            resolved = None
        if resolved:
            resolved_url = "".join(
                w.capitalize() for w in resolved.split() if w)
            if resolved_url and resolved_url != player_url_name:
                logger.info("Playerlist resolved %s \u2192 %s",
                            player_name, resolved)
                try:
                    url = f"{BASE_URL}/cgi-bin/{cgi}?p={resolved_url}"
                    resp = self._make_request(url)
                    if resp and "No player found" not in resp.text:
                        matches = self._parse_matches_from_html(resp.text)
                        if matches:
                            logger.info(
                                "Found %d matches in HTML for %s "
                                "(resolved)", len(matches), resolved)
                            return matches
                except Exception as exc:
                    logger.warning(
                        "Resolved HTML fetch failed for %s: %s",
                        resolved_url, exc)
                # JS fallback for the resolved name too
                for js_url in (
                    f"{BASE_URL}/jsmatches/{resolved_url}.js",
                    f"{BASE_URL}/jsmatches/{resolved_url}Career.js",
                ):
                    try:
                        resp = self._make_request(js_url, raise_on_error=False)
                        if resp and resp.status_code == 200:
                            matches = self._parse_matches_from_js(resp.text)
                            if matches:
                                logger.info(
                                    "Found %d matches from JS for %s "
                                    "(resolved): %s",
                                    len(matches), resolved, js_url)
                                return matches
                    except Exception as exc:
                        logger.warning("Could not get JS matches from %s: %s",
                                       js_url, exc)

        logger.warning("No matches found for %s", player_name)
        return None

    def _parse_matches_from_html(self, html_content):
        """Extract the matchmx JS array from HTML page."""
        try:
            marker = "var matchmx = ["
            start = html_content.find(marker)
            if start == -1:
                return None
            start += len(marker) - 1  # include the '['
            end = html_content.find("];", start)
            if end == -1:
                return None
            matches_str = html_content[start:end + 1]
            matches_str = matches_str.replace("null", "None")
            return ast.literal_eval(matches_str)
        except Exception as exc:
            logger.error("Error parsing HTML matches: %s", exc)
            return None

    def _fetch_matches_from_html_scripts(self, html_content):
        """Fetch matchmx data from jsmatches scripts linked by an HTML page."""
        soup = BeautifulSoup(html_content, "html.parser")
        seen = set()
        found_linked_js = False
        for script in soup.find_all("script", src=True):
            src = script.get("src") or ""
            if "jsmatches/" not in src:
                continue
            found_linked_js = True
            if src.startswith("//"):
                url = "https:" + src
            elif src.startswith("http"):
                url = src
            elif src.startswith("/"):
                url = BASE_URL + src
            else:
                url = f"{BASE_URL}/{src.lstrip('/')}"
            if url in seen:
                continue
            seen.add(url)
            try:
                resp = self._make_request(url, raise_on_error=False)
                if resp and resp.status_code == 200:
                    matches = self._parse_matches_from_js(resp.text)
                    if matches:
                        return matches, True
            except Exception as exc:
                logger.warning("Could not get HTML-linked JS matches from %s: %s",
                               url, exc)
        return [], found_linked_js

    def _parse_matches_from_js(self, js_content):
        """Parse the matchmx array from a JS file."""
        try:
            if "matchmx = [" in js_content:
                raw = js_content.split("matchmx = [")[1].split("];")[0]
                raw = "[" + raw + "]"
                raw = raw.replace("null", "None")
                return ast.literal_eval(raw)
            return []
        except Exception as exc:
            logger.error("Error parsing JS matches: %s", exc)
            return []

    def scrape_rankings_live_tennis(self, tour="atp", discipline="singles",
                                    source="LIVE"):
        """
        Scrape rankings from live-tennis.eu.

        Parameters
        ----------
        tour : str
            "atp" or "wta"
        discipline : str
            "singles" or "doubles"
        source : str
            "LIVE", "OFFICIAL" or "RACE"

        Returns list of dicts with:
            rank, name, country, points, age,
            rank_diff, pts_diff, next_tournament, tour
        """
        tour = (tour or "atp").lower()
        discipline = (discipline or "singles").lower()
        source = (source or "LIVE").upper()

        path_map = {
            ("atp", "singles", "LIVE"): "atp-live-ranking",
            ("wta", "singles", "LIVE"): "wta-live-ranking",
            ("atp", "doubles", "LIVE"): "atp-doubles-live-ranking",
            ("wta", "doubles", "LIVE"): "wta-doubles-live-ranking",
            ("atp", "singles", "OFFICIAL"): "official-atp-ranking",
            ("wta", "singles", "OFFICIAL"): "official-wta-ranking",
            ("atp", "doubles", "OFFICIAL"): "official-atp-doubles-ranking",
            ("wta", "doubles", "OFFICIAL"): "official-wta-doubles-ranking",
            ("atp", "singles", "RACE"): "atp-race",
            ("wta", "singles", "RACE"): "wta-race",
            ("atp", "doubles", "RACE"): "atp-doubles-race",
            ("wta", "doubles", "RACE"): "wta-doubles-race",
        }
        path = path_map.get((tour, discipline, source))
        if not path:
            logger.warning("Unsupported ranking request: %s %s %s",
                           tour, discipline, source)
            return []

        url = f"https://live-tennis.eu/en/{path}"
        results = []

        try:
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(url, timeout=30)
            resp.encoding = "utf-8"
            if resp.status_code != 200:
                logger.warning("live-tennis.eu returned %d", resp.status_code)
                return results

            soup = BeautifulSoup(resp.text, "html.parser")
            tables = soup.find_all("table")
            if not tables:
                return results

            # Pick table that contains most ranking rows (first td starts with rank)
            target = None
            best_rows = -1
            for tbl in tables:
                rows = 0
                for tr in tbl.find_all("tr"):
                    tds = tr.find_all("td")
                    if not tds:
                        continue
                    first = tds[0].get_text(strip=True)
                    if re.match(r"^\d+", first):
                        rows += 1
                if rows > best_rows:
                    best_rows = rows
                    target = tbl
            if target is None:
                return results

            # live pages include 2 extra leading cols vs official pages
            # LIVE row shape: rank,chg,*,name,age,ioc,points,...
            # OFFICIAL row shape: rank,*,name,age,ioc,points,...
            name_idx = 3 if source == "LIVE" else 2
            age_idx = name_idx + 1
            country_idx = name_idx + 2
            points_idx = name_idx + 3

            for tr in target.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) <= points_idx:
                    continue

                rank_text = tds[0].get_text(strip=True)
                if not rank_text or not rank_text[0].isdigit():
                    continue
                rank = int(re.sub(r"[^\d]", "", rank_text))

                raw_name = tds[name_idx].get_text(strip=True)
                raw_name = unicodedata.normalize("NFKC", raw_name)
                raw_name = re.sub(r"\s+", " ", raw_name).strip()

                age_text = tds[age_idx].get_text(strip=True)
                age = int(age_text) if age_text.isdigit() else None

                country = tds[country_idx].get_text(strip=True).upper()
                # Normalise IOC (3-letter)
                m = re.search(r"[A-Z]{3}", country)
                country = m.group(0) if m else country[:3]

                pts_text = re.sub(r"[^\d]", "",
                                  tds[points_idx].get_text(strip=True))
                points = int(pts_text) if pts_text else 0

                # Parse deltas from semantic classes, not positional indices.
                # This avoids false matches from unrelated columns (e.g. chtd).
                rank_diff = None
                pts_diff = None

                signed_cells = []
                for i, td in enumerate(tds):
                    txt = td.get_text(strip=True)
                    if not re.fullmatch(r"[+\-]\d+", txt or ""):
                        continue
                    classes = set(td.get("class", []))
                    signed_cells.append((i, txt, classes))

                # Rank diff: prefer rdf class (present on both LIVE/OFFICIAL when provided).
                for _, txt, classes in signed_cells:
                    if "rdf" in classes:
                        try:
                            rank_diff = int(txt)
                        except ValueError:
                            pass
                        break

                # On OFFICIAL pages some rows miss rdf but keep a trailing signed rank delta.
                if source == "OFFICIAL" and rank_diff is None and signed_cells:
                    try:
                        rank_diff = int(signed_cells[-1][1])
                    except ValueError:
                        pass

                # Points diff: LIVE uses sgr/srd classes; OFFICIAL does not expose this reliably.
                if source == "LIVE":
                    for _, txt, classes in signed_cells:
                        if "sgr" in classes or "srd" in classes:
                            try:
                                pts_diff = int(txt)
                            except ValueError:
                                pass
                            break

                # Tournament info: OFFICIAL pages have fixed columns
                # for current and previous tournament activity.
                current_tourn = ""
                previous_tourn = ""
                next_tourn = ""

                if source == "OFFICIAL":
                    # OFFICIAL rows contain several empty/sign/numeric cells
                    # after points before the tournament text.  Skip those
                    # separators; otherwise top players such as Sinner can get
                    # an empty fingerprint even when the row says "Madrid W".
                    tournament_cells = []
                    for td in tds[points_idx + 1:]:
                        txt = td.get_text(" ", strip=True)
                        txt = re.sub(r"\s+", " ", txt).strip()
                        if not txt:
                            continue
                        if re.fullmatch(r"[+\-]?\d+", txt):
                            continue
                        tournament_cells.append(txt)
                    if tournament_cells:
                        current_tourn = tournament_cells[0]
                    if len(tournament_cells) > 1:
                        previous_tourn = tournament_cells[1]
                    next_tourn = current_tourn or previous_tourn
                else:
                    # LIVE pages: first non-numeric trailing text
                    for idx in range(points_idx + 2, min(len(tds), points_idx + 6)):
                        txt = tds[idx].get_text(strip=True)
                        if txt and not re.fullmatch(r"[+\-]?\d+", txt):
                            next_tourn = txt
                            break

                results.append({
                    "rank": rank,
                    "name": raw_name,
                    "country": country,
                    "points": points,
                    "age": age,
                    "rank_diff": rank_diff,
                    "pts_diff": pts_diff,
                    "next_tournament": next_tourn,
                    "current_tournament": current_tourn,
                    "previous_tournament": previous_tourn,
                    "tour": tour,
                    "discipline": discipline,
                    "source": source,
                })

        except Exception as exc:
            logger.warning("Error scraping live-tennis.eu: %s", exc)

        logger.info("Total live rankings scraped: %d", len(results))
        return results

    def scrape_live_rankings(self, tour="atp"):
        """Backward-compatible wrapper for ATP/WTA live singles."""
        return self.scrape_rankings_live_tennis(
            tour=tour, discipline="singles", source="LIVE"
        )

    def fetch_rankings_html(self, tour="atp"):
        """Fetch current rankings page from tennisabstract."""
        tour = tour.lower()
        url = f"{BASE_URL}/reports/{tour}Rankings.html"
        try:
            resp = self._make_request(url)
            return resp.text if resp else None
        except Exception as exc:
            logger.error("Failed to fetch rankings: %s", exc)
            return None

    def parse_rankings(self, html, tour="atp"):
        """
        Parse rankings HTML table into a list of dicts.

        Returns list of: {rank, name, country, points}
        """
        if not html:
            return []

        try:
            soup = BeautifulSoup(html, "html.parser")
            tables = soup.find_all("table")

            target = None
            headers = []
            for tbl in tables:
                ths = tbl.find_all("th")
                if not ths:
                    continue
                labels = [th.get_text(" ", strip=True).lower() for th in ths]
                canon = ["".join(c for c in h if c.isalnum()) for h in labels]
                if any("rank" in h or h == "rk" for h in canon) and \
                   any("player" in h or "name" in h for h in canon):
                    target = tbl
                    headers = labels
                    break

            if target is None:
                if tables:
                    target = tables[0]
                    headers = [th.get_text(" ", strip=True).lower()
                               for th in target.find_all("th")]
                else:
                    return []

            canon_headers = ["".join(c for c in h if c.isalnum())
                             for h in headers]

            def find_idx(*keys):
                for k in keys:
                    for i, h in enumerate(canon_headers):
                        if k in h:
                            return i
                return None

            idx_rank = find_idx("rank", "rk")
            idx_name = find_idx("player", "name")
            idx_nat = find_idx("nat", "country", "ctry")
            idx_pts = find_idx("point", "pts")

            out = []
            for tr in target.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 2:
                    continue

                rank_text = (tds[idx_rank].get_text(strip=True)
                             if idx_rank is not None and idx_rank < len(tds)
                             else "")
                if not rank_text or not rank_text[0].isdigit():
                    continue
                try:
                    rank = int(re.sub(r"[^\d]", "", rank_text))
                except ValueError:
                    continue

                name_cell = (tds[idx_name]
                             if idx_name is not None and idx_name < len(tds)
                             else None)
                if not name_cell:
                    continue
                a = name_cell.find("a")
                name = (a.get_text(strip=True) if a
                        else name_cell.get_text(strip=True))
                # Normalize unicode whitespace (\xa0 etc.)
                name = unicodedata.normalize("NFKD", name)
                name = re.sub(r"\s+", " ", name).strip()

                country = ""
                if idx_nat is not None and idx_nat < len(tds):
                    img = tds[idx_nat].find("img")
                    if img and (img.get("alt") or img.get("title")):
                        alt = (img.get("alt") or img.get("title") or "").strip().upper()
                        m = re.search(r"\b([A-Z]{3})\b", alt)
                        if m:
                            country = m.group(1)
                    if not country:
                        txt = tds[idx_nat].get_text(strip=True).upper()
                        m = re.search(r"\b([A-Z]{3})\b", txt)
                        if m:
                            country = m.group(1)

                points = 0
                if idx_pts is not None and idx_pts < len(tds):
                    pts_text = tds[idx_pts].get_text(strip=True)
                    pts_text = re.sub(r"[^\d]", "", pts_text)
                    if pts_text:
                        points = int(pts_text)

                out.append({
                    "rank": rank,
                    "name": name,
                    "country": country,
                    "points": points,
                    "tour": tour,
                })

            return out
        except Exception as exc:
            logger.exception("Failed to parse rankings: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Extended stats scraping (player-more.cgi)
    # ------------------------------------------------------------------

    EXTENDED_TABLES = [
        "winners-errors", "serve-speed", "pbp-stats",
        "mcp-serve", "mcp-return", "mcp-rally",
    ]

    # Cached lookup: normalized name → official tennisabstract name
    # (built lazily from playerlist.js / wplayerlist.js)
    _PLAYERLIST_CACHE = {"atp": None, "wta": None}

    @staticmethod
    def _de_loose_key(norm_name):
        """Reduce German-style transliterations so that a stripped-diacritic
        name matches an umlaut-expanded one.

        Tennisabstract sometimes uses German style (Möller → Moeller) while
        our _clean_name strips diacritics (Möller → Moller).  This collapses
        oe→o, ue→u, ae→a, ss→s so both forms map to the same loose key.
        """
        s = norm_name
        s = s.replace("oe", "o").replace("ue", "u")
        s = s.replace("ae", "a").replace("ss", "s")
        return s

    def _load_playerlist(self, tour="atp"):
        """Fetch and cache the official tennisabstract playerlist for a tour.

        Returns a dict mapping normalized name → official name, where the
        official name's URL form (no spaces) is the actual jsfrags filename.
        Also includes a ``__loose__`` sub-dict for umlaut-tolerant lookups.
        """
        if self._PLAYERLIST_CACHE.get(tour) is not None:
            return self._PLAYERLIST_CACHE[tour]
        fname = "playerlist.js" if tour == "atp" else "wplayerlist.js"
        url = f"{BASE_URL}/jsplayers/{fname}"
        index = {}
        loose = {}
        try:
            resp = self._make_request(url, raise_on_error=False)
            if resp:
                # Extract the JS array literal: var playerlist=[...];
                m = re.search(r'=\s*\[(.*?)\]\s*;?\s*$',
                              resp.text, re.DOTALL)
                if m:
                    arr = ast.literal_eval("[" + m.group(1) + "]")
                    for full in arr:
                        if not isinstance(full, str):
                            continue
                        cleaned = self._clean_name(full)
                        norm = re.sub(r"\s+", " ", cleaned).lower().strip()
                        if norm and norm not in index:
                            index[norm] = cleaned
                        # Loose key: collapse German digraphs
                        lkey = self._de_loose_key(norm)
                        if lkey and lkey not in loose:
                            loose[lkey] = cleaned
        except Exception as exc:
            logger.warning("Could not load %s playerlist: %s", tour, exc)
        index["__loose__"] = loose
        self._PLAYERLIST_CACHE[tour] = index
        logger.info("Loaded %s playerlist: %d entries (%d loose)",
                    tour, len(index) - 1, len(loose))
        return index

    def _resolve_via_playerlist(self, player_name, tour="atp"):
        """Try to resolve a player name via tennisabstract's playerlist.js.

        Useful when the direct URL (e.g. SantiagoTaverna) doesn't exist
        but the player is registered under a longer name
        (e.g. Santiago Fa Rodriguez Taverna).

        Returns the cleaned official name (e.g. "Santiago Fa Rodriguez
        Taverna") or None if no match is found.
        """
        index = self._load_playerlist(tour)
        if not index:
            return None
        loose = index.get("__loose__", {})
        cleaned = self._clean_name(player_name)
        norm = re.sub(r"\s+", " ", cleaned).lower().strip()
        # 1. Exact match
        if norm in index and norm != "__loose__":
            return index[norm]
        # 2. Loose (German digraph) match — handles Möller/Moeller mismatch
        lkey = self._de_loose_key(norm)
        if lkey in loose:
            return loose[lkey]
        # 3. Hyphen-collapsed match (e.g. "han na chang" → "hanna chang")
        # _clean_name turns 'Han-na Chang' into 'Han na Chang' but TA may
        # store the same player as 'Hanna Chang' (no space).  Try every
        # adjacent-pair concatenation against the index.
        parts = norm.split()
        if len(parts) >= 2:
            for i in range(len(parts) - 1):
                collapsed = (" ".join(parts[:i])
                             + (" " if i else "")
                             + parts[i] + parts[i + 1]
                             + (" " if i + 2 < len(parts) else "")
                             + " ".join(parts[i + 2:])).strip()
                if collapsed in index and collapsed != "__loose__":
                    return index[collapsed]
                lkey2 = self._de_loose_key(collapsed)
                if lkey2 in loose:
                    return loose[lkey2]
        # 4. First word + last word match (handles compound surnames)
        if len(parts) >= 2:
            first = parts[0]
            last = parts[-1]
            l_first = self._de_loose_key(first)
            l_last = self._de_loose_key(last)
            candidates = []
            for k, v in index.items():
                if k == "__loose__":
                    continue
                kp = k.split()
                if len(kp) >= 2 and kp[0] == first and kp[-1] == last:
                    candidates.append(v)
            if not candidates:
                # Loose first+last match
                for k, v in loose.items():
                    kp = k.split()
                    if (len(kp) >= 2 and kp[0] == l_first
                            and kp[-1] == l_last):
                        candidates.append(v)
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                logger.info(
                    "Ambiguous playerlist match for %s: %d candidates",
                    player_name, len(candidates))
        return None

    def _discover_player_id(self, player_url_name, original_name=None,
                            tour="atp"):
        """Discover the numeric player_id from the jsfrags JS file.

        Returns (player_id, jsfrags_text) — player_id may be None if the
        jsfrags file doesn't contain player-more.cgi links, but the raw
        jsfrags text is still returned so the caller can fall back to
        parsing tables directly from it.

        If the direct jsfrags URL is missing or doesn't contain a
        player_id, falls back to the official playerlist.js to find the
        correct URL name.
        """
        url = f"{BASE_URL}/jsfrags/{player_url_name}.js"
        try:
            resp = self._make_request(url, raise_on_error=False)
            if resp:
                text = resp.text
                m = re.search(r'player-more\.cgi\?p=(\d+)/', text)
                if m:
                    return m.group(1), text
            else:
                text = None
        except Exception as exc:
            logger.warning("Could not discover player_id for %s: %s",
                           player_url_name, exc)
            text = None

        # Fallback: resolve via official playerlist
        if original_name:
            resolved = self._resolve_via_playerlist(original_name, tour=tour)
            if resolved:
                resolved_url = "".join(
                    w.capitalize() for w in resolved.split() if w)
                if resolved_url and resolved_url != player_url_name:
                    logger.info("Playerlist resolved %s → %s",
                                player_url_name, resolved_url)
                    url2 = f"{BASE_URL}/jsfrags/{resolved_url}.js"
                    try:
                        resp2 = self._make_request(url2, raise_on_error=False)
                        if resp2:
                            text2 = resp2.text
                            m2 = re.search(
                                r'player-more\.cgi\?p=(\d+)/', text2)
                            if m2:
                                return m2.group(1), text2
                            return None, text2
                    except Exception as exc:
                        logger.warning(
                            "Could not fetch resolved jsfrags for %s: %s",
                            resolved_url, exc)
        return None, text

    def _make_player_url_name(self, player_name):
        """Convert a player name to URL format (e.g. 'Jannik Sinner' -> 'JannikSinner')."""
        clean = self._clean_name(player_name)
        return "".join(w.capitalize() for w in clean.split() if w)

    def _make_player_hyphen_name(self, player_name):
        """Convert name to hyphenated format (e.g. 'Jannik Sinner' -> 'Jannik-Sinner')."""
        clean = self._clean_name(player_name)
        return "-".join(w.capitalize() for w in clean.split() if w)

    def _parse_table_from_soup(self, soup_table, player_name, table_name):
        """Parse headers and rows from a BeautifulSoup <table> element."""
        headers = []
        thead = soup_table.find("thead")
        if thead:
            for th in thead.find_all("th"):
                headers.append(th.get_text(strip=True).replace("\xa0", " "))
        else:
            header_row = soup_table.find("tr")
            if header_row:
                for th in header_row.find_all(["th", "td"]):
                    headers.append(
                        th.get_text(strip=True).replace("\xa0", " "))

        if not headers:
            return None

        rows = []
        tbody = soup_table.find("tbody")
        data_rows = (tbody.find_all("tr") if tbody
                     else soup_table.find_all("tr")[1:])
        for tr in data_rows:
            cells = tr.find_all("td")
            if len(cells) < 3:
                continue
            row = {}
            for i, cell in enumerate(cells):
                if i < len(headers):
                    row[headers[i]] = cell.get_text(strip=True).replace(
                        "\xa0", " ")
            rows.append(row)

        logger.info("Parsed %d rows from %s for %s",
                    len(rows), table_name, player_name)
        return rows

    def fetch_extended_table(self, player_name, table_name, _pid=None,
                             tour="atp"):
        """
        Fetch one extended data table from player-more.cgi.

        Parameters
        ----------
        player_name : str
            Player name (e.g. "Jannik Sinner")
        table_name : str
            One of EXTENDED_TABLES (e.g. "winners-errors")
        _pid : str or None
            Pre-discovered numeric player_id (avoids re-fetching jsfrags).
        tour : str
            "atp" or "wta"

        Returns list of dicts (one per match row), or None on failure.
        """
        url_name = self._make_player_url_name(player_name)
        hyphen_name = self._make_player_hyphen_name(player_name)

        if not _pid:
            _pid, _ = self._discover_player_id(
                url_name, original_name=player_name, tour=tour)
        if not _pid:
            logger.warning("Could not find player_id for %s", player_name)
            return None

        cgi = "wplayer-more.cgi" if tour == "wta" else "player-more.cgi"
        url = (f"{BASE_URL}/cgi-bin/{cgi}"
               f"?p={_pid}/{hyphen_name}&table={table_name}")
        try:
            resp = self._make_request(url, raise_on_error=False)
            if not resp:
                return None

            # Data table is inside a JS template literal: var player_frag = `...`
            frag_match = re.search(
                r'var\s+player_frag\s*=\s*`(.*?)`', resp.text, re.DOTALL)
            if frag_match:
                soup = BeautifulSoup(frag_match.group(1), "html.parser")
            else:
                soup = BeautifulSoup(resp.text, "html.parser")

            table = soup.find("table")
            if not table:
                logger.warning("No table found for %s/%s", player_name, table_name)
                return None

            return self._parse_table_from_soup(table, player_name, table_name)

        except Exception as exc:
            logger.warning("Error fetching %s for %s: %s",
                           table_name, player_name, exc)
            return None

    def _parse_tables_from_jsfrags(self, jsfrags_text, player_name, table_names):
        """Parse extended stats tables directly from jsfrags JS content.

        Some players' jsfrags files don't include player-more.cgi links but
        DO embed all the extended stats tables in ``var player_frag = `...```.
        This method extracts those tables by their HTML ``id`` attribute.
        """
        frag_match = re.search(
            r'var\s+player_frag\s*=\s*`(.*?)`', jsfrags_text, re.DOTALL)
        if not frag_match:
            logger.info(
                "[ext-diag] %s: no player_frag block in jsfrags "
                "(will fall back to player-more.cgi for all tables)",
                player_name)
            return {}
        soup = BeautifulSoup(frag_match.group(1), "html.parser")

        results = {}
        diag = []
        for table_name in table_names:
            table_el = soup.find("table", id=table_name)
            if not table_el:
                diag.append(f"{table_name}=missing")
                continue
            rows = self._parse_table_from_soup(table_el, player_name, table_name)
            if rows:
                results[table_name] = rows
                diag.append(f"{table_name}={len(rows)}")
            else:
                diag.append(f"{table_name}=0")
        logger.info("[ext-diag] %s jsfrags tables: %s",
                    player_name, ", ".join(diag) if diag else "(none)")
        return results

    def fetch_all_extended_tables(self, player_name, tables=None,
                                  progress_callback=None, tour="atp"):
        """
        Fetch all (or specified) extended data tables for a player.

        Strategy:
          1. If we resolved a numeric ``player_id``, fetch each table from
             ``player-more.cgi`` (full 52-week / Match Charting dataset).
          2. Otherwise (jsfrags has no ``player-more.cgi`` link — typical
             for new / unranked players that tennisabstract has not yet
             assigned a pid to), fall back to parsing the truncated tables
             embedded directly in the jsfrags ``var player_frag = `...```
             block (~20 most recent matches per table).

        Returns dict: { "winners-errors": [rows], "serve-speed": [rows], ... }
        """
        if tables is None:
            tables = self.EXTENDED_TABLES

        url_name = self._make_player_url_name(player_name)
        pid, jsfrags_text = self._discover_player_id(
            url_name, original_name=player_name, tour=tour)

        results = {}
        source = None  # "cgi" or "jsfrags-fallback"
        if pid:
            source = "cgi"
            for i, table_name in enumerate(tables):
                if progress_callback:
                    progress_callback(
                        i, len(tables),
                        f"Fetching {table_name} for {player_name}...")
                rows = self.fetch_extended_table(
                    player_name, table_name, _pid=pid, tour=tour)
                if rows:
                    results[table_name] = rows
        elif jsfrags_text:
            # No pid → use the (truncated) embedded tables as a fallback.
            source = "jsfrags-fallback"
            logger.info(
                "%s: no player-more.cgi pid available; using truncated "
                "jsfrags-embedded tables (recent matches only)",
                player_name)
            results = self._parse_tables_from_jsfrags(
                jsfrags_text, player_name, tables)
        else:
            logger.warning(
                "Could not fetch jsfrags for %s, all %d tables missing",
                player_name, len(tables))

        # Diagnostic summary
        summary = ", ".join(f"{t}={len(results[t])}"
                            for t in tables if t in results)
        missing_after = [t for t in tables if t not in results]
        if missing_after:
            summary = (summary + "; missing=" + ",".join(missing_after)
                       if summary else "missing=" + ",".join(missing_after))
        logger.info("[ext-diag] %s summary (%s): %s", player_name,
                    source or "none", summary or "(no tables)")

        if progress_callback:
            progress_callback(len(tables), len(tables),
                              f"Extended stats complete for {player_name}")
        return results

    # ------------------------------------------------------------------
    # Historical ranking snapshots (per-week back-fill)
    # ------------------------------------------------------------------

    def scrape_atp_history_snapshot(self, date_str, top_n=500):
        """Fetch ATP official singles ranking for a specific Monday.

        date_str: 'YYYY-MM-DD' (Monday).
        Returns list of dicts: {rank, name, country, points, tour}.
        Uses atptour.com which supports ?dateWeek=YYYY-MM-DD&rankRange=1-N
        in a single request (top_n up to ~5000 in one page).
        """
        url = (f"https://www.atptour.com/en/rankings/singles"
               f"?rankRange=1-{int(top_n)}&dateWeek={date_str}")
        scraper = cloudscraper.create_scraper()
        try:
            resp = scraper.get(url, timeout=30)
        except Exception as exc:
            logger.warning("ATP history fetch failed for %s: %s",
                           date_str, exc)
            return []
        if resp.status_code != 200:
            logger.warning("ATP history %s returned %d",
                           date_str, resp.status_code)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return []
        target = tables[0]
        results = []
        for tr in target.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            rank_txt = tds[0].get_text(strip=True).rstrip(".")
            if not rank_txt.isdigit():
                continue
            a = tr.find("a", href=lambda h: h and "/players/" in h)
            if not a:
                continue
            href = a.get("href", "")
            name = a.get_text(" ", strip=True)
            # href shape: /en/players/jannik-sinner/s0ag/overview
            slug_parts = href.strip("/").split("/")
            atp_pid = slug_parts[3] if len(slug_parts) > 3 else ""
            slug = slug_parts[2] if len(slug_parts) > 2 else ""
            full_name = slug.replace("-", " ").title() if slug else name
            pts_txt = tds[2].get_text(strip=True).replace(",", "")
            try:
                points = int(pts_txt) if pts_txt.isdigit() else None
            except ValueError:
                points = None
            results.append({
                "rank": int(rank_txt),
                "name": full_name,
                "country": "",
                "points": points,
                "tour": "atp",
                "source_id": atp_pid,
            })
        return results

    def scrape_wta_history_snapshot(self, date_str, top_n=500,
                                    page_sleep=1.5):
        """Fetch WTA singles ranking for a specific Monday from
        tennisexplorer.com.

        date_str: 'YYYY-MM-DD'.  TE paginates 50 rows/page → top_n=500
        means 10 sequential GETs.
        Returns list of dicts: {rank, name, country, points, tour}.
        """
        per_page = 50
        pages = max(1, (int(top_n) + per_page - 1) // per_page)
        results = []
        scraper = cloudscraper.create_scraper()
        for p in range(1, pages + 1):
            url = (f"https://www.tennisexplorer.com/ranking/wta-women/"
                   f"?date={date_str}&page={p}")
            try:
                resp = scraper.get(url, timeout=30)
            except Exception as exc:
                logger.warning("TE WTA %s p%d failed: %s",
                               date_str, p, exc)
                break
            if resp.status_code != 200:
                logger.warning("TE WTA %s p%d returned %d",
                               date_str, p, resp.status_code)
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            target = None
            for tbl in soup.find_all("table", class_="result"):
                rows = tbl.find_all("tr")
                if 30 < len(rows) < 80:
                    target = tbl
                    break
            if target is None:
                logger.warning("TE WTA %s p%d: ranking table not found",
                               date_str, p)
                break
            for tr in target.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 5:
                    continue
                rank_txt = tds[0].get_text(strip=True).rstrip(".")
                if not rank_txt.isdigit():
                    continue
                # Player cell is td[2] for TE
                name_cell = tds[2]
                a = name_cell.find("a")
                # TE format: "Sabalenka Aryna" → swap to "Aryna Sabalenka"
                raw = a.get_text(" ", strip=True) if a else \
                    name_cell.get_text(" ", strip=True)
                # Strip "(YYYY)" disambig suffix if present
                raw = re.sub(r"\s*\(\d{4}\)\s*$", "", raw).strip()
                parts = raw.split()
                if len(parts) >= 2:
                    # Last token is first name, rest is surname
                    first = parts[-1]
                    last = " ".join(parts[:-1])
                    full_name = f"{first} {last}"
                else:
                    full_name = raw
                country = tds[3].get_text(strip=True)
                pts_txt = tds[4].get_text(strip=True).replace(",", "")
                try:
                    points = int(pts_txt) if pts_txt.isdigit() else None
                except ValueError:
                    points = None
                slug = ""
                if a and a.get("href"):
                    m = re.search(r"/player/([^/]+)/?", a["href"])
                    if m:
                        slug = m.group(1)
                results.append({
                    "rank": int(rank_txt),
                    "name": full_name,
                    "country": country,
                    "points": points,
                    "tour": "wta",
                    "source_id": slug,
                })
                if len(results) >= top_n:
                    break
            if len(results) >= top_n:
                break
            if p < pages:
                time.sleep(page_sleep)
        return results


# ---------------------------------------------------------------------------
# Extended stats row conversion helpers
# ---------------------------------------------------------------------------

def _safe_float(val):
    """Parse a float from a string, returning None on failure."""
    if val is None or val == "" or val == "-" or val == "N/A":
        return None
    try:
        # Remove % signs
        val = str(val).replace("%", "").strip()
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val):
    if val is None or val == "" or val == "-" or val == "N/A":
        return None
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _extract_match_context(row):
    """Extract common match context fields from a raw extended stats row."""
    ctx = {}
    for key in row:
        kl = key.lower().strip()
        if kl == "match":
            val = row[key].replace("\xa0", " ").strip()
            # Format: "2026 Miami Masters F" or "2025 Roland Garros R128"
            # Extract year, tournament name, and round
            m = re.match(
                r"(\d{4})\s+(.+?)\s+"
                r"(F|SF|QF|R(?:R|16|32|64|128)|R\d+)$", val)
            if m:
                ctx["tourney_date"] = m.group(1)
                ctx["tourney_name"] = m.group(2).strip()
                ctx["round"] = m.group(3)
            else:
                # Fallback: at least extract the year
                m2 = re.match(r"(\d{4})\s+(.*)", val)
                if m2:
                    ctx["tourney_date"] = m2.group(1)
                    ctx["tourney_name"] = m2.group(2).strip()
        elif kl == "result":
            val = row[key].replace("\xa0", " ").strip()
            # Format: "W vs Lehecka" or "L vs Alcaraz"
            m = re.match(r"([WL])\s+vs\s*(.*)", val)
            if m:
                ctx["result"] = m.group(1)
                ctx["opponent_name"] = m.group(2).strip()
        elif kl in ("date", "dt"):
            ctx["tourney_date"] = row[key]
        elif kl in ("tournament", "tourn", "tourney"):
            ctx["tourney_name"] = row[key]
        elif kl in ("round", "rd"):
            ctx["round"] = row[key]
        elif kl in ("surface", "surf"):
            ctx["surface"] = row[key]
        elif kl in ("opponent", "opp"):
            ctx["opponent_name"] = row[key]
        elif kl in ("score", "sc"):
            ctx["score"] = row[key]
    return ctx


def convert_winners_errors(rows, player_name, tour="atp"):
    """Convert raw winners-errors rows to DB format.

    Actual headers from tennisabstract: Match, Result, Winners, UFEs,
    Ratio, Wnr/Pt, UFE/Pt, RallyWinners, RallyUFEs, RallyRatio,
    Rally Wnr/Pt, Rally UFE/Pt, FH Wnr/Pt, BH Wnr/Pt, vs Ratio,
    vs Wnr/Pt, vs UFE/Pt
    """
    records = []
    for row in rows:
        ctx = _extract_match_context(row)
        record = {
            "player_name": player_name,
            "opponent_name": ctx.get("opponent_name"),
            "tourney_name": ctx.get("tourney_name"),
            "tourney_date": ctx.get("tourney_date"),
            "round": ctx.get("round"),
            "surface": ctx.get("surface"),
            "score": ctx.get("score"),
            "tour": tour,
        }
        for key, val in row.items():
            kl = key.lower().strip()
            # Player stats (exact header match to avoid "rally*" columns)
            if kl == "winners":
                record["winners"] = _safe_int(val)
            elif kl == "ufes":
                record["unforced_errors"] = _safe_int(val)
            elif kl == "ratio":
                record["w_ue_ratio"] = _safe_float(val)
            elif kl == "wnr/pt":
                record["winner_pct"] = _safe_float(val)
            elif kl == "ufe/pt":
                record["ue_pct"] = _safe_float(val)
            # Opponent stats (vs prefix)
            elif kl == "vs ratio":
                record["opp_w_ue_ratio"] = _safe_float(val)
            elif kl == "vs wnr/pt":
                record["opp_winner_pct"] = _safe_float(val)
            elif kl == "vs ufe/pt":
                record["opp_ue_pct"] = _safe_float(val)
        records.append(record)
    return records


def convert_serve_speed(rows, player_name, tour="atp"):
    """Convert raw serve-speed rows to DB format.

    Actual headers: Match, Result, Avg Speed, 1st Avg, 1st StDev,
    1st T Avg, 1st Wide Avg, Max 1st, Min 1st, 2nd Avg, 2nd StDev,
    2nd T Avg, 2nd Wide Avg, Max 2nd, Min 2nd
    """
    records = []
    for row in rows:
        ctx = _extract_match_context(row)
        record = {
            "player_name": player_name,
            "opponent_name": ctx.get("opponent_name"),
            "tourney_name": ctx.get("tourney_name"),
            "tourney_date": ctx.get("tourney_date"),
            "round": ctx.get("round"),
            "surface": ctx.get("surface"),
            "speed_unit": "mph",
            "tour": tour,
        }
        for key, val in row.items():
            kl = key.lower().strip()
            # Use exact matches to avoid "1st t avg"/"1st wide avg" overwrites
            if kl == "1st avg":
                record["first_serve_avg"] = _safe_float(val)
            elif kl == "1st stdev":
                record["first_serve_stdev"] = _safe_float(val)
            elif kl == "max 1st":
                record["first_serve_max"] = _safe_float(val)
            elif kl == "min 1st":
                record["first_serve_min"] = _safe_float(val)
            elif kl == "2nd avg":
                record["second_serve_avg"] = _safe_float(val)
            elif kl == "2nd stdev":
                record["second_serve_stdev"] = _safe_float(val)
            elif kl == "max 2nd":
                record["second_serve_max"] = _safe_float(val)
            elif kl == "min 2nd":
                record["second_serve_min"] = _safe_float(val)
        records.append(record)
    return records


def convert_pbp_stats(rows, player_name, tour="atp"):
    """Convert raw pbp-stats rows to DB format.

    Actual headers: Match, Result, BLR, DR+, EI, CBF, Deuce A%,
    Deuce SPW%, Ad A%, Ad SPW%, Deuce RPW%, Ad RPW%
    """
    records = []
    for row in rows:
        ctx = _extract_match_context(row)
        record = {
            "player_name": player_name,
            "opponent_name": ctx.get("opponent_name"),
            "tourney_name": ctx.get("tourney_name"),
            "tourney_date": ctx.get("tourney_date"),
            "round": ctx.get("round"),
            "surface": ctx.get("surface"),
            "tour": tour,
        }
        for key, val in row.items():
            kl = key.lower().strip()
            # BLR = Baseline Rally Length
            if kl == "blr":
                record["rally_length_avg"] = _safe_float(val)
            # DR+ = Dominance Ratio
            elif kl == "dr+":
                record["aggressive_margin"] = _safe_float(val)
            # EI = Efficiency Index
            elif kl == "ei":
                record["serve_plus1_ratio"] = _safe_float(val)
            # Deuce RPW% = return points won on deuce court
            elif kl == "deuce rpw%":
                record["return_pts_won_pct"] = _safe_float(val)
        records.append(record)
    return records


def convert_mcp_serve(rows, player_name, tour="atp"):
    """Convert raw mcp-serve rows to DB format.

    Actual headers: Match, Result, Unret%, <=3 W%, RiP W%, SvImpact,
    1st: Unret%, <=3 W%, RiP W%, SvImpact, D Wide%, A Wide%, BP Wide%,
    2nd: Unret%, <=3 W%, RiP W%, 2ndAgg
    """
    records = []
    for row in rows:
        ctx = _extract_match_context(row)
        record = {
            "player_name": player_name,
            "opponent_name": ctx.get("opponent_name"),
            "tourney_name": ctx.get("tourney_name"),
            "tourney_date": ctx.get("tourney_date"),
            "round": ctx.get("round"),
            "surface": ctx.get("surface"),
            "tour": tour,
        }
        for key, val in row.items():
            kl = key.lower().strip()
            if kl == "unret%":
                record["unreturned_pct"] = _safe_float(val)
            elif kl == "d wide%":
                record["deuce_wide_pct"] = _safe_float(val)
            elif kl == "a wide%":
                record["ad_wide_pct"] = _safe_float(val)
        records.append(record)
    return records


def convert_mcp_return(rows, player_name, tour="atp"):
    """Convert raw mcp-return rows to DB format.

    Actual headers: Match, Result, RiP%, RiP W%, RetWnr%, FH/BH,
    RDI, Slice%, 1st: RiP%, RiP W%, RetWnr%, RDI, Slice%,
    2nd: RiP%, RiP W%, RetWnr%, RDI, Slice%
    """
    records = []
    for row in rows:
        ctx = _extract_match_context(row)
        record = {
            "player_name": player_name,
            "opponent_name": ctx.get("opponent_name"),
            "tourney_name": ctx.get("tourney_name"),
            "tourney_date": ctx.get("tourney_date"),
            "round": ctx.get("round"),
            "surface": ctx.get("surface"),
            "tour": tour,
        }
        for key, val in row.items():
            kl = key.lower().strip()
            if kl == "rip%":
                record["return_in_play_pct"] = _safe_float(val)
            elif kl == "1st: rip%":
                record["first_return_in_play_pct"] = _safe_float(val)
            elif kl == "2nd: rip%":
                record["second_return_in_play_pct"] = _safe_float(val)
        records.append(record)
    return records


def convert_mcp_rally(rows, player_name, tour="atp"):
    """Convert raw mcp-rally rows to DB format.

    Actual headers: Match, Result, RallyLen, RLen-Serve, RLen-Return,
    1-3 W%, 4-6 W%, 7-9 W%, 10+ W%, FH/GS, BH Slice%, FHP,
    FHP/100, BHP, BHP/100
    """
    records = []
    for row in rows:
        ctx = _extract_match_context(row)
        record = {
            "player_name": player_name,
            "opponent_name": ctx.get("opponent_name"),
            "tourney_name": ctx.get("tourney_name"),
            "tourney_date": ctx.get("tourney_date"),
            "round": ctx.get("round"),
            "surface": ctx.get("surface"),
            "tour": tour,
        }
        for key, val in row.items():
            kl = key.lower().strip()
            if kl == "rallylen":
                record["rally_length_avg"] = _safe_float(val)
            elif kl == "1-3 w%":
                record["rally_won_0_4"] = _safe_float(val)
            elif kl == "4-6 w%":
                record["rally_won_5_8"] = _safe_float(val)
            elif kl == "10+ w%":
                record["rally_won_9_plus"] = _safe_float(val)
            elif kl == "bh slice%":
                record["bh_pct"] = _safe_float(val)
        records.append(record)
    return records


def convert_mcp_tactics(rows, player_name, tour="atp"):
    """Convert raw mcp-tactics rows to DB format."""
    records = []
    for row in rows:
        ctx = _extract_match_context(row)
        record = {
            "player_name": player_name,
            "opponent_name": ctx.get("opponent_name"),
            "tourney_name": ctx.get("tourney_name"),
            "tourney_date": ctx.get("tourney_date"),
            "round": ctx.get("round"),
            "surface": ctx.get("surface"),
            "tour": tour,
        }
        for key, val in row.items():
            kl = key.lower().strip()
            if "net" in kl and ("approach" in kl or "appr" in kl) and "won" not in kl:
                record["net_approach_pct"] = _safe_float(val)
            elif "net" in kl and "won" in kl:
                record["net_points_won_pct"] = _safe_float(val)
            elif "drop" in kl and "won" not in kl:
                record["dropshot_pct"] = _safe_float(val)
            elif "drop" in kl and "won" in kl:
                record["dropshot_won_pct"] = _safe_float(val)
            elif "s&v" in kl or "serve" in kl and "volley" in kl:
                if "won" in kl:
                    record["sv_won_pct"] = _safe_float(val)
                else:
                    record["serve_and_volley_pct"] = _safe_float(val)
            elif "inside" in kl and "in" in kl:
                record["inside_in_fh_pct"] = _safe_float(val)
            elif "inside" in kl and "out" in kl:
                record["inside_out_fh_pct"] = _safe_float(val)
        records.append(record)
    return records


# Table name -> converter function mapping
EXTENDED_CONVERTERS = {
    "winners-errors": convert_winners_errors,
    "serve-speed": convert_serve_speed,
    "pbp-stats": convert_pbp_stats,
    "mcp-serve": convert_mcp_serve,
    "mcp-return": convert_mcp_return,
    "mcp-rally": convert_mcp_rally,
    "mcp-tactics": convert_mcp_tactics,
}


def clean_player_name(name):
    """Module-level wrapper around ``TennisAbstractScraper._clean_name``.

    Used by callers (data_manager, UI pages, DB migrations) to ensure a
    consistent, diacritic-free spelling of player names when storing or
    looking up rows in the extended-stats tables and cache.
    """
    if name is None:
        return None
    return TennisAbstractScraper._clean_name(name)


# Table name -> DB table name mapping
EXTENDED_DB_TABLES = {
    "winners-errors": "match_winners_errors",
    "serve-speed": "match_serve_speed",
    "pbp-stats": "match_pbp_stats",
    "mcp-serve": "match_mcp_serve",
    "mcp-return": "match_mcp_return",
    "mcp-rally": "match_mcp_rally",
    "mcp-tactics": "match_mcp_tactics",
}


def convert_scraped_to_db_format(raw_matches, player_name, min_year=None,
                                 max_matches=None, tour="atp"):
    """
    Convert raw scraped match arrays into a DataFrame matching the
    existing database schema (winner/loser format).

    Parameters
    ----------
    raw_matches : list
        List of match arrays from tennisabstract
    player_name : str
        The name of the player whose matches these are
    min_year : int or None
        If set, discard matches before this year (e.g. 2025).
    max_matches : int or None
        If set, keep only the *max_matches* most recent matches
        (after the min_year filter).  Useful for incremental refreshes
        when the player already has historical data in the DB.

    Returns
    -------
    pd.DataFrame with columns matching the 'matches' table schema
    """
    if not raw_matches:
        return pd.DataFrame()

    # Pre-compute year cutoff string for fast comparison (e.g. "20250000")
    min_date_str = str(min_year * 10000) if min_year else None

    # When max_matches is set, sort raw matches by date desc and trim early.
    # raw_matches[i][0] is the date as int/str like 20250416.
    if max_matches is not None and max_matches > 0:
        def _date_key(m):
            try:
                return int(m[0])
            except (ValueError, TypeError, IndexError):
                return 0
        raw_matches = sorted(raw_matches, key=_date_key, reverse=True)
        # Keep extra headroom (3x) before filtering, in case some get dropped
        # for being walkovers / pre-min_year / parse errors.
        raw_matches = raw_matches[: max_matches * 3]

    records = []
    for match in raw_matches:
        try:
            # Extract fields using index mapping
            row = {}
            for idx, col_name in MATCH_COLUMNS.items():
                if idx < len(match):
                    row[col_name] = match[idx]
                else:
                    row[col_name] = None

            # Parse date
            date_raw = row.get("date")
            if not date_raw:
                continue
            try:
                date_str = str(int(date_raw))
            except (ValueError, TypeError):
                date_str = str(date_raw)

            # Skip matches before min_year
            if min_date_str and date_str < min_date_str:
                continue

            # Determine winner/loser based on W/L flag
            wl = str(row.get("wl", "")).upper()
            opponent = row.get("opp", "")
            score = row.get("score", "")

            # Walkovers (no data, no future meaning) are still skipped.
            if score == "W/O":
                continue
            # Empty score = upcoming/scheduled match (not yet played).
            score_is_empty = (
                score in ("", None)
                or (isinstance(score, float) and pd.isna(score))
            )
            is_upcoming = score_is_empty and bool(opponent)

            surface = row.get("surf", "")
            tourney = row.get("tourn", "")
            round_ = row.get("round", "")
            level = row.get("level", "")
            orank = row.get("orank")
            pseed = row.get("pseed")
            pentry = (row.get("pentry") or "").strip() or None
            oseed = row.get("oseed")
            oentry = (row.get("oentry") or "").strip() or None
            oioc = (row.get("oioc") or "").strip() or None

            # Parse numeric stats safely
            def safe_num(val):
                if val is None or val == "" or val == "None":
                    return None
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return None

            minutes = safe_num(row.get("minutes"))

            prank = row.get("prank")

            if is_upcoming:
                # Scheduled match: store player as placeholder winner so
                # name-based queries still find it; the is_upcoming flag
                # is what callers should filter on.
                winner_name = player_name
                loser_name = opponent
                winner_rank = safe_num(prank)
                loser_rank = safe_num(orank)
                winner_seed = pseed
                winner_entry = pentry
                loser_seed = oseed
                loser_entry = oentry
                winner_ioc = None
                loser_ioc = oioc
                w_ace = w_df = w_svpt = w_1stIn = None
                w_1stWon = w_2ndWon = w_SvGms = None
                w_bpSaved = w_bpFaced = None
                l_ace = l_df = l_svpt = l_1stIn = None
                l_1stWon = l_2ndWon = l_SvGms = None
                l_bpSaved = l_bpFaced = None
            elif wl == "W":
                winner_name = player_name
                loser_name = opponent
                winner_rank = safe_num(prank)
                loser_rank = safe_num(orank)
                winner_seed = pseed
                winner_entry = pentry
                loser_seed = oseed
                loser_entry = oentry
                winner_ioc = None  # filled later from players table
                loser_ioc = oioc
                # Player serve stats = winner stats
                w_ace = safe_num(row.get("aces"))
                w_df = safe_num(row.get("dfs"))
                w_svpt = safe_num(row.get("svpt"))
                w_1stIn = safe_num(row.get("first_in"))
                w_1stWon = safe_num(row.get("first_won"))
                w_2ndWon = safe_num(row.get("second_won"))
                w_SvGms = safe_num(row.get("sv_gms"))
                w_bpSaved = safe_num(row.get("bp_saved"))
                w_bpFaced = safe_num(row.get("bp_faced"))
                # Opponent serve stats = loser stats
                l_ace = safe_num(row.get("o_aces"))
                l_df = safe_num(row.get("o_dfs"))
                l_svpt = safe_num(row.get("o_svpt"))
                l_1stIn = safe_num(row.get("o_first_in"))
                l_1stWon = safe_num(row.get("o_first_won"))
                l_2ndWon = safe_num(row.get("o_second_won"))
                l_SvGms = safe_num(row.get("o_sv_gms"))
                l_bpSaved = safe_num(row.get("o_bp_saved"))
                l_bpFaced = safe_num(row.get("o_bp_faced"))
            elif wl == "L":
                winner_name = opponent
                loser_name = player_name
                winner_rank = safe_num(orank)
                loser_rank = safe_num(prank)
                winner_seed = oseed
                winner_entry = oentry
                loser_seed = pseed
                loser_entry = pentry
                winner_ioc = oioc
                loser_ioc = None  # filled later from players table
                # Opponent is winner
                w_ace = safe_num(row.get("o_aces"))
                w_df = safe_num(row.get("o_dfs"))
                w_svpt = safe_num(row.get("o_svpt"))
                w_1stIn = safe_num(row.get("o_first_in"))
                w_1stWon = safe_num(row.get("o_first_won"))
                w_2ndWon = safe_num(row.get("o_second_won"))
                w_SvGms = safe_num(row.get("o_sv_gms"))
                w_bpSaved = safe_num(row.get("o_bp_saved"))
                w_bpFaced = safe_num(row.get("o_bp_faced"))
                # Player is loser
                l_ace = safe_num(row.get("aces"))
                l_df = safe_num(row.get("dfs"))
                l_svpt = safe_num(row.get("svpt"))
                l_1stIn = safe_num(row.get("first_in"))
                l_1stWon = safe_num(row.get("first_won"))
                l_2ndWon = safe_num(row.get("second_won"))
                l_SvGms = safe_num(row.get("sv_gms"))
                l_bpSaved = safe_num(row.get("bp_saved"))
                l_bpFaced = safe_num(row.get("bp_faced"))
            else:
                continue  # skip unknown result

            # Map surface names for consistency
            surf_map = {"H": "Hard", "C": "Clay", "G": "Grass", "P": "Carpet"}
            surface_full = surf_map.get(surface, surface)

            records.append({
                "tourney_id": "",
                "tourney_name": tourney,
                "surface": surface_full,
                "draw_size": None,
                "tourney_level": LEVEL_MAP.get(level, level),
                "tourney_date": date_str,
                "match_num": None,
                "winner_id": "",
                "winner_seed": winner_seed,
                "winner_entry": winner_entry,
                "winner_name": winner_name,
                "winner_hand": None,
                "winner_ht": None,
                "winner_ioc": winner_ioc,
                "winner_age": None,
                "loser_id": "",
                "loser_seed": loser_seed,
                "loser_entry": loser_entry,
                "loser_name": loser_name,
                "loser_hand": None,
                "loser_ht": None,
                "loser_ioc": loser_ioc,
                "loser_age": None,
                "score": score,
                "best_of": None,
                "round": round_,
                "minutes": minutes,
                "w_ace": w_ace,
                "w_df": w_df,
                "w_svpt": w_svpt,
                "w_1stIn": w_1stIn,
                "w_1stWon": w_1stWon,
                "w_2ndWon": w_2ndWon,
                "w_SvGms": w_SvGms,
                "w_bpSaved": w_bpSaved,
                "w_bpFaced": w_bpFaced,
                "l_ace": l_ace,
                "l_df": l_df,
                "l_svpt": l_svpt,
                "l_1stIn": l_1stIn,
                "l_1stWon": l_1stWon,
                "l_2ndWon": l_2ndWon,
                "l_SvGms": l_SvGms,
                "l_bpSaved": l_bpSaved,
                "l_bpFaced": l_bpFaced,
                "winner_rank": winner_rank,
                "winner_rank_points": None,
                "loser_rank": loser_rank,
                "loser_rank_points": None,
                "tour": tour,
                "is_upcoming": 1 if is_upcoming else 0,
            })

        except Exception as exc:
            logger.warning("Error converting match: %s", exc)
            continue

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    # Final trim to exactly max_matches most recent rows (we may have
    # kept 3x headroom above to absorb walkover/parse drops).
    if max_matches is not None and max_matches > 0 and len(df) > max_matches:
        df = df.sort_values("tourney_date", ascending=False).head(max_matches)
        df = df.reset_index(drop=True)
    return df
