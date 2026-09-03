#!/usr/bin/env python3
"""LT Metrics launcher.

Starts the web app and opens it in your browser.

    python run.py                # http://127.0.0.1:8000
    python run.py --port 9000    # custom port
    python run.py --no-browser   # don't auto-open

First-time setup:
    pip install -r requirements.txt
"""
from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser


def _open_browser(url: str) -> None:
    time.sleep(1.5)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _find_free_port(host: str, start: int, tries: int = 20) -> int | None:
    for p in range(start, start + tries):
        if _port_is_free(host, p):
            return p
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the LT Metrics web app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    try:
        import uvicorn  # noqa
    except ImportError:
        raise SystemExit("Dependencies missing. Run:  pip install -r requirements.txt")

    port = args.port
    if not _port_is_free(args.host, port):
        alt = _find_free_port(args.host, port + 1)
        if alt is None:
            raise SystemExit(
                f"Port {port} is in use and no free port was found nearby.\n"
                f"Another LT Metrics may already be running — close it, or run:\n"
                f"    python run.py --port 9000")
        print(f"[LT Metrics] Port {port} is busy (another LT Metrics instance?). "
              f"Using {alt} instead.")
        port = alt

    url = f"http://{args.host}:{port}"
    print("=" * 60)
    print("  LT Metrics — Load Testing Reimagined")
    print(f"  Open: {url}")
    print("=" * 60)
    if not args.no_browser:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    import uvicorn
    uvicorn.run("ltmetrics.server:app", host=args.host, port=port, reload=False)


if __name__ == "__main__":
    main()
