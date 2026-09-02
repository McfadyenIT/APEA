#!/usr/bin/env bash
# ===================================================================
#  LT Metrics - one-click launcher (macOS / Linux)
#  Run:  ./start.sh   (chmod +x start.sh the first time)
# ===================================================================
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.9+ is required but was not found."
  echo "Install it from https://www.python.org/downloads/ and run ./start.sh again."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating a private Python environment (first run only)..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing / updating dependencies (first run may take a minute)..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo ""
echo "Starting LT Metrics - your browser will open at http://127.0.0.1:8000"
echo "Press Ctrl+C to stop LT Metrics."
echo ""
python run.py
