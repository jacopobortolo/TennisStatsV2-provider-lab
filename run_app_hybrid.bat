@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\TennisStatsV2\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set MATCH_PROVIDER=hybrid
set SOFASCORE_API_BASE_URL=http://127.0.0.1:8765/api/v1
"%PY%" -m tennis_app
pause