@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\TennisStatsV2\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m tennis_app
pause
