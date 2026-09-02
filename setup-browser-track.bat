@echo off
REM ==================================================================
REM  LT Metrics - enable the real-browser (Playwright) track  [one-time]
REM  Double-click this once to install Playwright + Chromium into the
REM  same private environment LT Metrics uses. Safe to re-run; it just checks
REM  and updates. Needed only for the Track-B card-payment (Stripe /
REM  CyberSource) testing - normal load tests do NOT need this.
REM ==================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\" (
    echo.
    echo LT Metrics's environment isn't set up yet. Run start.bat once first,
    echo then run this again.
    echo.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

echo Installing the Playwright Python package...
python -m pip install --quiet -r requirements-browser.txt
if errorlevel 1 (
    echo.
    echo Failed to install Playwright. Check your internet connection and retry.
    echo.
    pause
    exit /b 1
)

echo Downloading the Chromium browser Playwright drives (~150 MB, first time only)...
python -m playwright install chromium
if errorlevel 1 (
    echo.
    echo Playwright installed, but the Chromium download failed. You can retry with:
    echo     .venv\Scripts\python -m playwright install chromium
    echo.
    pause
    exit /b 1
)

echo.
echo Done. The real-browser (Playwright) track is now enabled.
echo Start LT Metrics (start.bat), then run a test with the browser track turned on
echo (browser VUs greater than 0 + a browser payment gateway) to drive real card iframes.
echo.
pause
