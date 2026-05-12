@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\TennisStatsV2\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 2; if ($r.status -eq 'ok') { exit 0 } exit 1 } catch { exit 1 }"
if not %ERRORLEVEL% EQU 0 (
    echo SofaScore proxy is not running.
    echo Start run_sofascore_proxy.bat first, then run this file again.
    pause
    exit /b 1
)
set MATCH_PROVIDER=hybrid
set SOFASCORE_API_BASE_URL=http://127.0.0.1:8765/api/v1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%PY%" -m tennis_app.cron --top 150 --no-extended --min-year 2026 --match-provider hybrid
pause