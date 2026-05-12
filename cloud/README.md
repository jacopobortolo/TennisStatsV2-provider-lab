# Cloud setup — GitHub Actions + Turso

The cloud component keeps the Turso database fresh 24/7 so the desktop app always has up-to-date data when you open it — even if your PC was off.

```
┌─────────────────────────┐   every 4h   ┌──────────────────┐
│ GitHub Actions          │ ────────────► │  Turso (libSQL)  │
│  cloud/scrape_job.py    │              │  cloud DB        │
│  top-1000 players       │              └────────┬─────────┘
└─────────────────────────┘                       │
                                                  │ sync on startup
                                                  ▼
                                       ┌──────────────────┐
                                       │ Desktop app      │
                                       │ python -m        │
                                       │ tennis_app       │
                                       └──────────────────┘
```

## Free-tier usage

| Service | Limit | Actual usage |
|---------|-------|-------------|
| Turso | 500 DBs · 9 GB · 1 B row reads/mo | ~50 MB DB, ~5 M reads/mo |
| GitHub Actions | 2,000 min/mo (private repo) | ~30 min/run × 180 runs/mo ≈ 5,400 min |

> For private repos the free tier is 2,000 min/mo. At 4-hour frequency with ~30 min/run that is ~5,400 min/mo — slightly over the free limit. Upgrade to GitHub Team ($4/mo) or reduce to `*/6` (every 6 hours) to stay free.

---

## One-time setup

### 1. Create the Turso database

```powershell
winget install ChiselStrike.Turso
turso auth login
turso db create tennis-stats
turso db tokens create tennis-stats --expiration none
turso db show tennis-stats --url
```

Save the URL (`libsql://tennis-stats-<org>.turso.io`) and the token.

### 2. Add GitHub secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|------|-------|
| `TURSO_DATABASE_URL` | `libsql://tennis-stats-...turso.io` |
| `TURSO_AUTH_TOKEN` | the long token from the CLI |

### 3. Enable the workflow

Push `.github/workflows/scrape.yml` to GitHub. The cron (`7 */4 * * *`) starts automatically. Trigger one manually first to verify:

**Actions → 4-Hourly Tennis Scrape → Run workflow**

The first run scrapes all 1,000 players from scratch (~30–60 min). Subsequent runs are much faster because the activity fingerprint skips players with no new matches.

---

## How the scrape job works

1. **Fetches live rankings** (ATP + WTA top 1000) from LiveTennis
2. **Activity fingerprinting** — compares each player's `current_tournament|previous_tournament` string to what's stored in `scrape_cache`. Only players with a changed fingerprint are scraped.
3. **Parallel HTTP scraping** — 8 concurrent workers hit tennisabstract.com
4. **Incremental import** — imports only the 20 most recent matches per player (non-destructive: older rows are preserved)
5. **Extended stats** — serve speed, rally length, MCP stats scraped after the main pass
6. **Writes to Turso** — all data is written to the remote libSQL database

---

## Changing scrape frequency

Edit the `cron` line in `.github/workflows/scrape.yml`:

```yaml
- cron: '7 */4 * * *'   # every 4 hours (current)
- cron: '7 */6 * * *'   # every 6 hours (free tier safe)
- cron: '7 9,21 * * *'  # twice a day
- cron: '7 9 * * *'     # once a day
```

---

## Files

| Path | Purpose |
|------|---------|
| `cloud/scrape_job.py` | GitHub Actions entrypoint (`python -m cloud.scrape_job`) |
| `cloud/db.py` | Turso HTTP connection helpers |
| `cloud/sync.py` | Download Turso data → merge into local SQLite |
| `.github/workflows/scrape.yml` | 4-hourly cron workflow |
| `requirements-cloud.txt` | Server-side deps (no PySide6) |

---

## Troubleshooting

**Scrape takes longer than expected on first run**
Normal — the first run populates `scrape_cache` for all 1,000 players from scratch. Subsequent runs skip unchanged players and complete much faster.

**`KeyError: 'result'` in logs**
The libsql-client raises this for malformed queries or schema mismatches. Wrapped internally as `sqlite3.OperationalError`.

**Workflow times out**
The timeout is 350 minutes. If it still times out, reduce `top_n` via manual dispatch: **Run workflow → top_n: 500**.

**Turso token expired**
Re-generate: `turso db tokens create tennis-stats --expiration none` and update the GitHub secret.
