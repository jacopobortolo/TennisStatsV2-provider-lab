@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\TennisStatsV2\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "MATCH_PROVIDER=sofascore"
set "SOFASCORE_API_BASE_URL=http://127.0.0.1:8765/api/v1"
set "SOFASCORE_EVENT_PAGES=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "LOGDIR=%USERPROFILE%\.tennis_analytics\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%i"
set "LOGFILE=%LOGDIR%\sofascore_cloud_%STAMP%.log"

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 2; if ($r.status -eq 'ok') { exit 0 } exit 1 } catch { exit 1 }"
if not %ERRORLEVEL% EQU 0 (
    echo Starting SofaScore proxy...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PY%' -ArgumentList '-m','tennis_app.scripts.sofascore_proxy','--host','127.0.0.1','--port','8765' -WorkingDirectory '%CD%' -WindowStyle Minimized"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; for ($i=0; $i -lt 20; $i++) { try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 2; if ($r.status -eq 'ok') { $ok=$true; break } } catch {}; Start-Sleep -Seconds 1 }; if (-not $ok) { exit 1 }"
    if not %ERRORLEVEL% EQU 0 (
        echo SofaScore proxy did not become ready. See %LOGFILE%
        exit /b 1
    )
)

echo Writing log to %LOGFILE%
"%PY%" -m cloud.scrape_job --top 1000 --no-extended --min-year 2026 --match-provider sofascore --max-matches-per-player 10 > "%LOGFILE%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo SofaScore cloud scrape failed with exit code %RC%. See %LOGFILE%
    exit /b %RC%
)

echo SofaScore cloud scrape complete. See %LOGFILE%
exit /b 0