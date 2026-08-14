"""SQLite history ledger — the Post-Run Intelligence & RCA memory.

Stores projects, runs, and per-endpoint statistics so the platform can compare
against historical baselines, spot regressions, and answer natural-language
queries like "show all checkout regressions over the last six months".
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Optional

from .config import DB_PATH

_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_url TEXT NOT NULL,
                client TEXT,
                created_at TEXT NOT NULL,
                config_json TEXT,
                UNIQUE(name, target_url)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                project_id INTEGER,
                test_type TEXT,
                users INTEGER,
                spawn_rate REAL,
                duration_s INTEGER,
                started_at TEXT,
                finished_at TEXT,
                status TEXT,
                total_requests INTEGER DEFAULT 0,
                total_failures INTEGER DEFAULT 0,
                error_rate REAL DEFAULT 0,
                avg_response REAL DEFAULT 0,
                p95 REAL DEFAULT 0,
                throughput REAL DEFAULT 0,
                sla_pass INTEGER DEFAULT 0,
                output_dir TEXT,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                name TEXT,
                method TEXT,
                num_requests INTEGER,
                num_failures INTEGER,
                avg REAL, min REAL, max REAL,
                p50 REAL, p90 REAL, p95 REAL, p99 REAL,
                rps REAL,
                sla_target REAL,
                sla_pass INTEGER,
                FOREIGN KEY(run_id) REFERENCES runs(id)
            );

            -- Memory layer: what a run LEARNED, so the next run knows it without
            -- an LLM call. scope_type in (target|platform|global); scope_key is
            -- e.g. a host ('mcstaging.radwell.eu') or platform ('magento').
            CREATE TABLE IF NOT EXISTS learned_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                fact_value TEXT,
                source_run_id TEXT,
                confidence REAL DEFAULT 1.0,
                updated_at TEXT NOT NULL,
                UNIQUE(scope_type, scope_key, fact_key)
            );

            -- Per-run artifacts: the generated script, the final (post-repair)
            -- script, the errors seen, the repair applied and the root cause — so
            -- 'generated vs final' can be diffed and script-level fixes mined
            -- across many projects.
            CREATE TABLE IF NOT EXISTS run_artifacts (
                run_id TEXT PRIMARY KEY,
                project_id INTEGER,
                platform TEXT,
                generated_script TEXT,
                final_script TEXT,
                errors_json TEXT,
                repair_json TEXT,
                root_cause TEXT,
                created_at TEXT NOT NULL
            );

            -- Root-cause memory: error signature -> root cause + repair that
            -- resolved it, so recurring failures are auto-diagnosed.
            CREATE TABLE IF NOT EXISTS rca_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                project_id INTEGER,
                platform TEXT,
                error_signature TEXT NOT NULL,
                root_cause TEXT,
                repair_action TEXT,
                resolved INTEGER DEFAULT 0,
                occurrences INTEGER DEFAULT 1,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, error_signature)
            );
            """
        )
        # migration: optional user label for runs (rename in History)
        try:
            conn.execute("ALTER TABLE runs ADD COLUMN label TEXT")
        except Exception:
            pass


def rename_project(project_id: int, new_name: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE projects SET name=? WHERE id=?", (new_name, project_id))


def delete_project(project_id: int) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM endpoints WHERE run_id IN "
                     "(SELECT id FROM runs WHERE project_id=?)", (project_id,))
        conn.execute("DELETE FROM runs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def delete_run(run_id: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM endpoints WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM runs WHERE id=?", (run_id,))


def set_run_label(run_id: str, label: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE runs SET label=? WHERE id=?", (label, run_id))


def get_or_create_project(name: str, target_url: str, client: str = "",
                          config: Optional[dict] = None) -> int:
    now = datetime.utcnow().isoformat()
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT id FROM projects WHERE name=? AND target_url=?",
            (name, target_url),
        ).fetchone()
        if row:
            if config is not None:
                conn.execute(
                    "UPDATE projects SET config_json=? WHERE id=?",
                    (json.dumps(config), row["id"]),
                )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO projects(name, target_url, client, created_at, config_json)"
            " VALUES(?,?,?,?,?)",
            (name, target_url, client, now, json.dumps(config or {})),
        )
        return int(cur.lastrowid)


def create_run(run_id: str, project_id: int, test_type: str, users: int,
               spawn_rate: float, duration_s: int, output_dir: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs(id, project_id, test_type, users, spawn_rate,"
            " duration_s, started_at, status, output_dir)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, project_id, test_type, users, spawn_rate, duration_s,
             datetime.utcnow().isoformat(), "running", output_dir),
        )


def finalize_run(run_id: str, metrics: dict[str, Any], status: str = "completed") -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, status=?, total_requests=?, total_failures=?,"
            " error_rate=?, avg_response=?, p95=?, throughput=?, sla_pass=? WHERE id=?",
            (
                datetime.utcnow().isoformat(), status,
                metrics.get("total_requests", 0), metrics.get("total_failures", 0),
                metrics.get("error_rate", 0.0), metrics.get("avg_response", 0.0),
                metrics.get("p95", 0.0), metrics.get("throughput", 0.0),
                1 if metrics.get("sla_pass") else 0, run_id,
            ),
        )


def set_run_status(run_id: str, status: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))


def save_endpoints(run_id: str, endpoints: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM endpoints WHERE run_id=?", (run_id,))
        for e in endpoints:
            conn.execute(
                "INSERT INTO endpoints(run_id, name, method, num_requests, num_failures,"
                " avg, min, max, p50, p90, p95, p99, rps, sla_target, sla_pass)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, e.get("name"), e.get("method"), e.get("num_requests", 0),
                    e.get("num_failures", 0), e.get("avg", 0), e.get("min", 0),
                    e.get("max", 0), e.get("p50", 0), e.get("p90", 0), e.get("p95", 0),
                    e.get("p99", 0), e.get("rps", 0), e.get("sla_target", 0),
                    1 if e.get("sla_pass") else 0,
                ),
            )


def list_projects() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT p.*, COUNT(r.id) AS run_count, MAX(r.started_at) AS last_run"
            " FROM projects p LEFT JOIN runs r ON r.project_id = p.id"
            " GROUP BY p.id ORDER BY COALESCE(MAX(r.started_at), p.created_at) DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_runs(project_id: Optional[int] = None, limit: int = 100) -> list[dict]:
    with _LOCK, _connect() as conn:
        if project_id is not None:
            rows = conn.execute(
                "SELECT r.*, p.name AS project_name, p.target_url"
                " FROM runs r JOIN projects p ON p.id = r.project_id"
                " WHERE r.project_id=? ORDER BY r.started_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT r.*, p.name AS project_name, p.target_url"
                " FROM runs r JOIN projects p ON p.id = r.project_id"
                " ORDER BY r.started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_run(run_id: str) -> Optional[dict]:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT r.*, p.name AS project_name, p.target_url, p.client"
            " FROM runs r JOIN projects p ON p.id = r.project_id WHERE r.id=?",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        run = dict(row)
        eps = conn.execute(
            "SELECT * FROM endpoints WHERE run_id=? ORDER BY num_requests DESC",
            (run_id,),
        ).fetchall()
        run["endpoints"] = [dict(e) for e in eps]
        return run


def previous_baseline(project_id: int, test_type: str, before_run_id: str) -> Optional[dict]:
    """Most recent completed run of the same project + test type, for trend comparison."""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE project_id=? AND test_type=? AND status='completed'"
            " AND id != ? ORDER BY started_at DESC LIMIT 1",
            (project_id, test_type, before_run_id),
        ).fetchone()
        return dict(row) if row else None


def query_endpoint_history(name_like: str, months: int = 6) -> list[dict]:
    """Backing store for the natural-language history interface."""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT e.name, e.p95, e.num_requests, e.num_failures, e.sla_pass,"
            " r.test_type, r.started_at, p.name AS project_name, p.target_url"
            " FROM endpoints e JOIN runs r ON r.id = e.run_id"
            " JOIN projects p ON p.id = r.project_id"
            " WHERE lower(e.name) LIKE ? AND r.started_at >= date('now', ?)"
            " ORDER BY r.started_at DESC",
            (f"%{name_like.lower()}%", f"-{int(months)} months"),
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Memory layer — learned facts + RCA memory (see also apea/memory.py)
# --------------------------------------------------------------------------- #
def learn_fact(scope_type: str, scope_key: str, fact_key: str, fact_value: str,
               source_run_id: str = "", confidence: float = 1.0) -> None:
    """Persist something a run discovered (e.g. target 'x' -> region_policy=omit).
    Idempotent per (scope_type, scope_key, fact_key)."""
    now = datetime.utcnow().isoformat()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO learned_facts(scope_type, scope_key, fact_key, fact_value,"
            " source_run_id, confidence, updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(scope_type, scope_key, fact_key) DO UPDATE SET"
            " fact_value=excluded.fact_value, source_run_id=excluded.source_run_id,"
            " confidence=excluded.confidence, updated_at=excluded.updated_at",
            (scope_type, scope_key, fact_key, fact_value, source_run_id,
             float(confidence), now),
        )


def recall_facts(scope_type: str, scope_key: str) -> dict[str, str]:
    """All facts for a scope, as {fact_key: fact_value}."""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT fact_key, fact_value FROM learned_facts"
            " WHERE scope_type=? AND scope_key=?",
            (scope_type, scope_key),
        ).fetchall()
        return {r["fact_key"]: r["fact_value"] for r in rows}


def save_run_artifacts(run_id: str, project_id: Optional[int] = None,
                       platform: str = "", generated_script: Optional[str] = None,
                       final_script: Optional[str] = None,
                       errors: Optional[Any] = None, repair: Optional[Any] = None,
                       root_cause: Optional[str] = None) -> None:
    """Upsert per-run artifacts. Only non-None fields are written, so the
    generated script (saved at build time) and the final script + errors + root
    cause (saved at finalize) can be recorded in separate calls."""
    now = datetime.utcnow().isoformat()
    err_json = None if errors is None else json.dumps(errors)[:200000]
    rep_json = None if repair is None else json.dumps(repair)[:100000]
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO run_artifacts(run_id, project_id, platform, generated_script,"
            " final_script, errors_json, repair_json, root_cause, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id) DO UPDATE SET"
            " project_id=COALESCE(excluded.project_id, run_artifacts.project_id),"
            " platform=COALESCE(NULLIF(excluded.platform,''), run_artifacts.platform),"
            " generated_script=COALESCE(excluded.generated_script, run_artifacts.generated_script),"
            " final_script=COALESCE(excluded.final_script, run_artifacts.final_script),"
            " errors_json=COALESCE(excluded.errors_json, run_artifacts.errors_json),"
            " repair_json=COALESCE(excluded.repair_json, run_artifacts.repair_json),"
            " root_cause=COALESCE(excluded.root_cause, run_artifacts.root_cause)",
            (run_id, project_id, platform, generated_script, final_script,
             err_json, rep_json, root_cause, now),
        )


def get_run_artifacts(run_id: str) -> Optional[dict]:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM run_artifacts WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def record_rca(error_signature: str, platform: str = "", root_cause: str = "",
               repair_action: str = "", resolved: bool = False,
               run_id: str = "", project_id: Optional[int] = None) -> None:
    """Remember a root-cause + the repair that resolved it. Bumps occurrences on
    a repeat so recurring failures are visibly recurring."""
    now = datetime.utcnow().isoformat()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO rca_memory(run_id, project_id, platform, error_signature,"
            " root_cause, repair_action, resolved, occurrences, updated_at)"
            " VALUES(?,?,?,?,?,?,?,1,?)"
            " ON CONFLICT(platform, error_signature) DO UPDATE SET"
            " root_cause=excluded.root_cause, repair_action=excluded.repair_action,"
            " resolved=MAX(rca_memory.resolved, excluded.resolved),"
            " occurrences=rca_memory.occurrences+1, run_id=excluded.run_id,"
            " updated_at=excluded.updated_at",
            (run_id, project_id, platform, error_signature, root_cause,
             repair_action, 1 if resolved else 0, now),
        )


def recall_rca(error_signature: str, platform: str = "") -> Optional[dict]:
    """Prior diagnosis for an error signature (exact platform first, then any)."""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM rca_memory WHERE error_signature=? AND platform=?",
            (error_signature, platform),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM rca_memory WHERE error_signature=?"
                " ORDER BY resolved DESC, occurrences DESC LIMIT 1",
                (error_signature,),
            ).fetchone()
        return dict(row) if row else None
