"""Shared HTTP identity helpers for SofaScore traffic.

Proxy and provider should present the same request profile so the upstream
session keeps a coherent locale and browser fingerprint across requests.
"""

from __future__ import annotations

import os


SOFASCORE_HTTP_PROFILE_ENV = "SOFASCORE_HTTP_PROFILE"
SOFASCORE_USER_AGENT_ENV = "SOFASCORE_USER_AGENT"
SOFASCORE_ACCEPT_ENV = "SOFASCORE_ACCEPT"
SOFASCORE_ACCEPT_LANGUAGE_ENV = "SOFASCORE_ACCEPT_LANGUAGE"
SOFASCORE_ORIGIN_ENV = "SOFASCORE_ORIGIN"
SOFASCORE_REFERER_ENV = "SOFASCORE_REFERER"


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_DEFAULT_ACCEPT = "application/json,text/plain,*/*"
_DEFAULT_ORIGIN = "https://www.sofascore.com"
_DEFAULT_REFERER = "https://www.sofascore.com/tennis"

_PROFILE_DEFAULTS = {
    "global": {
        "accept_language": "en-US,en;q=0.9",
        "origin": _DEFAULT_ORIGIN,
        "referer": _DEFAULT_REFERER,
    },
    "en-us": {
        "accept_language": "en-US,en;q=0.9",
        "origin": _DEFAULT_ORIGIN,
        "referer": _DEFAULT_REFERER,
    },
    "it-ch": {
        "accept_language": "it-CH,it;q=0.9,de-CH;q=0.6,en;q=0.5",
        "origin": _DEFAULT_ORIGIN,
        "referer": _DEFAULT_REFERER,
    },
    "it-it": {
        "accept_language": "it-IT,it;q=0.9,en;q=0.7",
        "origin": _DEFAULT_ORIGIN,
        "referer": _DEFAULT_REFERER,
    },
}
_PROFILE_ALIASES = {
    "": "global",
    "default": "global",
    "global": "global",
    "en": "en-us",
    "en-us": "en-us",
    "us": "en-us",
    "it": "it-ch",
    "it-ch": "it-ch",
    "ch-it": "it-ch",
    "switzerland": "it-ch",
    "it-it": "it-it",
    "italy": "it-it",
}


def sofascore_http_profile_name() -> str:
    raw = str(os.getenv(SOFASCORE_HTTP_PROFILE_ENV, "global") or "global")
    normalized = raw.strip().lower().replace("_", "-")
    return _PROFILE_ALIASES.get(normalized, normalized if normalized in _PROFILE_DEFAULTS else "global")


def build_sofascore_headers() -> dict[str, str]:
    profile = _PROFILE_DEFAULTS[sofascore_http_profile_name()]
    return {
        "User-Agent": os.getenv(SOFASCORE_USER_AGENT_ENV, _DEFAULT_USER_AGENT),
        "Accept": os.getenv(SOFASCORE_ACCEPT_ENV, _DEFAULT_ACCEPT),
        "Accept-Language": os.getenv(
            SOFASCORE_ACCEPT_LANGUAGE_ENV, profile["accept_language"]),
        "Origin": os.getenv(SOFASCORE_ORIGIN_ENV, profile["origin"]),
        "Referer": os.getenv(SOFASCORE_REFERER_ENV, profile["referer"]),
    }


def describe_sofascore_headers(headers: dict[str, str] | None = None) -> str:
    payload = headers or build_sofascore_headers()
    return (
        f"profile={sofascore_http_profile_name()} "
        f"lang={payload.get('Accept-Language', '')} "
        f"referer={payload.get('Referer', '')}"
    )