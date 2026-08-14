# Running APEA as a standalone application

APEA is a fully **local** application — it runs on your machine, stores data in a
local SQLite file, and sends load only to the target you choose. There are three
ways to run it standalone, from easiest to most self-contained.

---

## Option 1 — One-click launcher (needs Python installed)  ✅ ready now

Double-click **`start.bat`** (Windows) or run **`./start.sh`** (macOS/Linux).

The launcher automatically:
1. creates a private virtual environment (`.venv`) on first run,
2. installs dependencies,
3. starts the app and opens `http://127.0.0.1:8000` in your browser.

This is the simplest option for any machine that already has Python 3.9+.
If Python isn't installed, the launcher tells the user where to get it.

---

## Option 2 — Docker (needs Docker installed)  ✅ ready now

```bash
docker build -t apea .
docker run -p 8000:8000 -v "%cd%/projects:/app/projects" apea   # Windows
docker run -p 8000:8000 -v "$PWD/projects:/app/projects" apea    # macOS/Linux
```
Then open `http://localhost:8000`. Fully self-contained; no Python needed on the
host, only Docker. See `Dockerfile` / `docker-compose.yml`.

---

## Option 3 — True no-Python .exe (PyInstaller)  🔧 requires one refactor

To ship a single `APEA.exe` that runs on a PC with **no Python at all**, two
things are required:

1. **Bundle with PyInstaller** — packages Python, all dependencies, Locust, and
   the web UI into one executable that launches the server and opens the browser.

2. **Run Locust in-process.** Today the Execution agent starts Locust as a
   subprocess (`python -m locust ...`). Inside a frozen `.exe` there is no
   `python` interpreter to call, so the executor must instead drive Locust
   through its Python API (`locust.env.Environment` + runners) and write the CSV
   stats in-process. Without this change the packaged app can crawl, plan, and
   generate scripts, but cannot *run* the load test.

Recommended build once the in-process executor is in place:

```bash
pip install pyinstaller
pyinstaller --noconfirm --clean APEA.spec
# result: dist/APEA/APEA.exe  (a folder you can zip and share)
```

A starter `APEA.spec` would include the `apea/static` folder and Locust's data
files as bundled resources, and set `run.py` as the entry point with
`--host 127.0.0.1 --no-browser` handled internally.

> This option is not enabled yet because it needs the in-process executor
> refactor. Ask and it can be added — it keeps the current `python run.py`
> workflow working exactly as-is and only changes behaviour when frozen.

---

## Which should I use?

| Need | Use |
|---|---|
| Quick local use, Python available | **Option 1** (`start.bat` / `start.sh`) |
| Reproducible / server / CI | **Option 2** (Docker) |
| Give to non-technical users with no Python | **Option 3** (PyInstaller `.exe`) |
