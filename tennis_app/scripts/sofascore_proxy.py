"""Local SofaScore-compatible proxy for the provider lab.

This script exposes a tiny FastAPI app with the same path shape used by
``SofaScoreMatchProvider``:

    /api/v1/search/all?q=...
    /api/v1/search/teams?q=...
    /api/v1/team/{team_id}/events/last/{page}

Run it locally, then point the lab provider to it with
``SOFASCORE_API_BASE_URL=http://127.0.0.1:8765/api/v1`` or with the
``--sofascore-api-base-url`` flag on ``probe_match_provider``.
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Any

import cloudscraper
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - optional lab dependency
    curl_requests = None

logger = logging.getLogger(__name__)

UPSTREAM_BASE = "https://www.sofascore.com/api/v1"

app = FastAPI(title="TennisStats SofaScore Proxy", version="0.1-lab")

_scraper = cloudscraper.create_scraper()
_scraper.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
})

_CACHE: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any]] = {}
_CACHE_SECONDS = 120


def _cache_key(path: str, params: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return path, tuple(sorted((str(k), str(v)) for k, v in params.items()))


def _upstream_get(path: str, params: dict[str, Any] | None = None) -> Any:
    params = params or {}
    key = _cache_key(path, params)
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]

    url = f"{UPSTREAM_BASE}{path}"
    try:
        if curl_requests is not None:
            response = curl_requests.get(
                url,
                params=params,
                headers=dict(_scraper.headers),
                timeout=25,
                impersonate="chrome124",
            )
        else:
            response = _scraper.get(url, params=params, timeout=25)
    except Exception as exc:
        logger.exception("SofaScore upstream request failed: %s", url)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if response.status_code == 403 and curl_requests is not None:
        # Occasionally curl_cffi gets challenged but cloudscraper can still
        # solve/handle the response. Keep this as a fallback for exploration.
        response = _scraper.get(url, params=params, timeout=25)

    if response.status_code >= 400:
        detail = response.text[:500]
        logger.warning(
            "SofaScore upstream %s returned %s: %s",
            url, response.status_code, detail,
        )
        raise HTTPException(status_code=response.status_code, detail=detail)

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON") from exc

    _CACHE[key] = (now, payload)
    return payload


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/search/all")
def search_all(q: str, page: int = 0) -> Any:
    return _upstream_get("/search/all", {"q": q, "page": page})


@app.get("/api/v1/search/teams")
def search_teams(q: str, page: int = 0) -> Any:
    return _upstream_get("/search/teams", {"q": q, "page": page})


@app.get("/api/v1/team/{team_id}/events/last/{page}")
def team_last_events(team_id: int, page: int = 0) -> Any:
    return _upstream_get(f"/team/{team_id}/events/last/{page}")


@app.get("/api/v1/{path:path}")
def passthrough(path: str, request: Request) -> Any:
    """Fallback for nearby SofaScore endpoints during exploration."""
    return _upstream_get(f"/{path}", dict(request.query_params))


@app.exception_handler(HTTPException)
def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run local SofaScore proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    import uvicorn

    uvicorn.run(
        "tennis_app.scripts.sofascore_proxy:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())