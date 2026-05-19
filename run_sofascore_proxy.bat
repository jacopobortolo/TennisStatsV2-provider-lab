@echo off
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\TennisStatsV2\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "SOFASCORE_HTTP_PROFILE=it-ch"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 2; if ($r.status -eq 'ok') { exit 0 } exit 1 } catch { exit 1 }"
if %ERRORLEVEL% EQU 0 (
	echo SofaScore proxy is already running on http://127.0.0.1:8765
	echo You can now start run_app_hybrid.bat.
	pause
	exit /b 0
)
"%PY%" -m tennis_app.scripts.sofascore_proxy --host 127.0.0.1 --port 8765
pause