"""Central configuration and path management for APEA.

Everything is isolated under the project root so the tool is fully portable.
Project artifacts follow the layout:
    projects/<Project_Name>/<url-slug>/<run-id>/{scripts,data,results,reports}
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Root = the folder that contains the `apea` package.
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a `.env` file in the project root into the
    environment, so users can set ANTHROPIC_API_KEY (etc.) without shell commands.

    Existing environment variables always win (never overwritten). Lines starting
    with '#' and blank lines are ignored; surrounding quotes are stripped.
    """
    env_path = BASE_DIR / ".env"
    try:
        if not env_path.exists():
            return
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass


# Load .env as early as possible (before llm.py / agents read os.environ).
_load_dotenv()
PROJECTS_DIR = BASE_DIR / "projects"
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = BASE_DIR / "uploads"
SAVED_DIR = BASE_DIR / "saved_scripts"
DB_PATH = BASE_DIR / "apea_history.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"

PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
SAVED_DIR.mkdir(parents=True, exist_ok=True)

# Default exit criteria applied when the user does not override them.
DEFAULT_ERROR_RATE_THRESHOLD = 1.0   # percent
DEFAULT_P95_THRESHOLD_MS = 3000.0    # milliseconds

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 APEA-Crawler/1.0"
)


def slugify(value: str) -> str:
    """Turn a URL or name into a filesystem-safe slug."""
    value = re.sub(r"^https?://", "", value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:60] or "target"


def project_run_dir(project_name: str, target_url: str, run_id: str) -> Path:
    """Return (and create) the isolated directory for a single test run."""
    p = PROJECTS_DIR / slugify(project_name) / slugify(target_url) / run_id
    (p / "scripts").mkdir(parents=True, exist_ok=True)
    (p / "data").mkdir(parents=True, exist_ok=True)
    (p / "results").mkdir(parents=True, exist_ok=True)
    (p / "reports").mkdir(parents=True, exist_ok=True)
    return p
