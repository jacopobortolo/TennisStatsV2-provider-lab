@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\TennisStatsV2\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "PYTHONUNBUFFERED=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo Tennis Analytics V2 — offline mode (local DB only, no Turso sync)

"%PY%" -u -m tennis_app --no-sync %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo App exited with error code %RC%.
) else (
    echo App exited normally.
)
pause
exit /b %RC%
