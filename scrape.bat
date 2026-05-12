@echo off
REM Headless scrape — runs without UI.
cd /d "%~dp0"
python -m tennis_app.cron --monday-boost
