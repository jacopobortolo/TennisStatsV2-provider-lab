@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\TennisStatsV2\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "PYTHONUNBUFFERED=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo SofaScore bulk scrape - requires local proxy on http://127.0.0.1:8765
echo.

set "PROXY_URL=http://127.0.0.1:8765/api/v1"
set "SOFASCORE_API_BASE_URL=%PROXY_URL%"

echo Usage: %~nx0  [--count N] [--tour atp] [--tour wta] [--cloud] [--sleep S]
echo.

"%PY%" -u -m tennis_app.scripts.sofascore_bulk_scrape %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo Bulk scrape exited with error code %RC%.
) else (
    echo Bulk scrape completed.
)
pause
exit /b %RC%
