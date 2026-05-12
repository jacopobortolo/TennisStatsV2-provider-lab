# TennisStatsV2

Desktop tennis analytics app built with PySide6. Combines historical match data from Jeff Sackmann's CSV datasets with live scraping from tennisabstract.com, kept fresh 24/7 by a GitHub Actions cron job writing to a Turso cloud database.

---

## Architecture

```
┌─────────────────────────┐   every 4h   ┌──────────────────┐
│ GitHub Actions          │ ────────────► │  Turso (libSQL)  │
│  cloud/scrape_job.py    │              │  cloud DB        │
│  top-1000 players       │              └────────┬─────────┘
└─────────────────────────┘                       │
                                                  │ sync on startup
                                                  ▼
┌─────────────────────────────────────────────────────────────┐
│ Desktop app  (python -m tennis_app)                         │
│                                                             │
│  splash: init DB → cloud sync → open UI                    │
│                                                             │
│  local SQLite  (~/.tennis_analytics/data/tennis.db)         │
│   ├── historical matches   (Sackmann CSVs, 1968–today)      │
│   ├── live matches         (scraped, tourney_id='SCRAPED')  │
│   ├── live rankings        (ranking_date='LIVE')            │
│   └── extended stats       (from cloud sync)                │
└─────────────────────────────────────────────────────────────┘
```

---

## Quick start

```powershell
git clone https://github.com/jacopobortolo/TennisStatsV2.git
cd TennisStatsV2
pip install -r requirements.txt
python -m tennis_app
```

**First launch on a new PC:**
1. The app initialises the local SQLite schema automatically
2. Cloud sync runs — downloads all scraped matches and live rankings from Turso
3. Historical CSV data (Sackmann, 1968–present) is downloaded and imported automatically (~300 MB, one-time)
4. All subsequent launches sync only the delta from cloud

To skip the cloud sync (offline mode):
```powershell
python -m tennis_app --no-sync
```

---

## Cloud scrape job (GitHub Actions)

See [`cloud/README.md`](cloud/README.md) for the one-time setup.

Key behaviour:
- Runs every **4 hours** (`cron: '7 */4 * * *'`)
- Scrapes the **top 1000 ATP + WTA players** from tennisabstract.com
- Uses **activity fingerprinting** (current/previous tournament from OFFICIAL rankings) to skip players with no new matches — only truly changed fingerprints trigger a re-scrape
- Imports only the **20 most recent matches** per player per run (incremental, non-destructive)
- Extended stats are included in the cloud DB and synced to the desktop at startup

---

## Pages

| Tab | Description |
|-----|-------------|
| **Rankings** | Live ATP/WTA rankings with filtering |
| **Player** | Player profile, stats, surface breakdown |
| **Matches** | Match history with surface/year/round/level filters |
| **H2H** | Head-to-head record between two players |
| **Stats** | Extended match statistics (serve speed, rally length, etc.) |
| **Tournaments** | Tournament history and results |

---

## Manual operations

```powershell
# Headless scrape (same pipeline as the UI button, no Qt needed)
python -m tennis_app.cron
```

**"Refresh Extended Stats" button** in the toolbar manually triggers a scrape of extended stats (serve speed, rally, MCP stats, etc.) for the top 150 players. Normally this data comes from the cloud sync — use this only if you need fresher data between syncs.

---

## Local cron (Windows Task Scheduler)

If you also want the PC to run scrapes locally:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

Creates a "TennisStats Hourly Scrape" task running `scrape.bat` every hour.

---

## Requirements

| Package | Version | Purpose |
|---------|---------|---------|
| PySide6 | ≥6.6.0 | Desktop UI |
| pandas | ≥2.0.0 | Data processing |
| requests | ≥2.28.0 | HTTP downloads |
| beautifulsoup4 | ≥4.12.0 | HTML parsing |
| cloudscraper | ≥1.2.0 | tennisabstract scraping (bypasses Cloudflare) |
| matplotlib | ≥3.7.0 | Charts |
| plotly | ≥5.18.0 | Interactive charts |
| libsql-client | ≥0.3.1 | Turso cloud DB (cloud sync only) |
