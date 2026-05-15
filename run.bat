@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\TennisStatsV2\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "PYTHONUNBUFFERED=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo Tennis Analytics V2
echo Working directory: %CD%
echo Python: %PY%
echo.

set "ARGS=%*"
if /I "%~1"=="--no-sync" (
	echo Starting app without cloud sync. Data may be stale.
) else (
	echo Starting app with Turso cloud sync before UI. Use run.bat --no-sync for offline startup.
)
echo.

if "%TENNIS_RUNBAT_DRY_RUN%"=="1" (
	echo Dry run: "%PY%" -u -m tennis_app %ARGS%
	exit /b 0
)

"%PY%" -u -m tennis_app %ARGS%
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
	echo App exited with error code %RC%.
) else (
	echo App exited normally.
)
pause
exit /b %RC%
