@echo off
REM ==================================================================
REM  LT Metrics - one-click launcher (Windows)
REM  Double-click this file. It sets up everything and opens the app.
REM ==================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo Python 3.9+ is required but was not found on this PC.
    echo Install it from https://www.python.org/downloads/  ^(tick "Add to PATH"^)
    echo then double-click start.bat again.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\" (
    echo Creating a private Python environment ^(first run only^)...
    python -m venv .venv
)

call ".venv\Scripts\activate.bat"

echo Installing / updating dependencies ^(first run may take a minute^)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

REM --- OPTIONAL real-browser (Playwright) track --------------------------
REM Kept OUT of the core install so normal load tests stay lightweight.
REM If Playwright is already installed (you ran setup-browser-track.bat),
REM make sure its Chromium browser is present - idempotent, quick if done.
REM Otherwise just print a one-line hint. Never blocks startup.
python -c "import playwright" >nul 2>nul
if not errorlevel 1 (
    echo Real-browser track detected - ensuring Chromium is installed...
    python -m playwright install chromium >nul 2>nul
) else (
    echo ^(Optional^) To enable real card-payment testing in a browser, run setup-browser-track.bat once.
)

echo.
echo Starting LT Metrics - your browser will open at http://127.0.0.1:8000
echo Close this window to stop LT Metrics.
echo.
python run.py

pause
