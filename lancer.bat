@echo off
cd /d "%~dp0"
title Repostly (local)

if not exist .venv (
  python -m venv .venv
  call .venv\Scripts\activate
  pip install -r requirements.txt
  python -m playwright install chromium
) else (
  call .venv\Scripts\activate
)

REM Mode local : pas de plafond Render
set SCRAPE_LIGHT=0
set SCRAPE_HEADLESS=1

echo.
echo  Repostly local → http://127.0.0.1:8787
echo  Mode archive : data\archives\
echo.
uvicorn server:app --reload --host 127.0.0.1 --port 8787
