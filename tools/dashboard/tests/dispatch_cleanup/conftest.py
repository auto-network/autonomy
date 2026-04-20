"""Fixtures for auto-8bnq0 — dispatch reconciliation + SSE pipeline tests.

Real dispatch.db + dashboard.db in tmp paths. No patches on _check_tmux, no
mocked HTTP (tests exercise real endpoints where applicable). Pattern
borrowed from tools/dashboard/tests/monitor_ipc/ (auto-opbyh).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("pytest_asyncio")


# ── dashboard.db schema + helpers ────────────────────────────────────


def init_dashboard_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tmux_sessions (
            tmux_name TEXT PRIMARY KEY,
            session_uuid TEXT,
            graph_source_id TEXT,
            type TEXT NOT NULL,
            project TEXT NOT NULL,
            jsonl_path TEXT,
            bead_id TEXT,
            created_at REAL NOT NULL,
            is_live INTEGER DEFAULT 1,
            file_offset INTEGER DEFAULT 0,
            last_activity REAL,
            last_message TEXT DEFAULT '',
            entry_count INTEGER DEFAULT 0,
            context_tokens INTEGER DEFAULT 0,
            label TEXT DEFAULT '',
            topics TEXT DEFAULT '[]',
            role TEXT DEFAULT '',
            nag_enabled INTEGER DEFAULT 0,
            nag_interval INTEGER DEFAULT 15,
            nag_message TEXT DEFAULT '',
            nag_last_sent REAL DEFAULT 0,
            dispatch_nag INTEGER DEFAULT 0,
            resolution_dir TEXT,
            session_uuids TEXT DEFAULT '[]',
            curr_jsonl_file TEXT,
            activity_state TEXT DEFAULT 'idle'
        )
        """
    )
    conn.commit()
    conn.close()


# ── dispatch.db schema + helpers ─────────────────────────────────────


def init_dispatch_db(db_path: Path) -> None:
    """Create a minimal dispatch_runs schema matching the real one."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatch_runs (
            id TEXT PRIMARY KEY,
            bead_id TEXT,
            started_at DATETIME,
            completed_at DATETIME,
            duration_secs INTEGER,
            status TEXT,
            reason TEXT,
            failure_category TEXT,
            commit_hash TEXT,
            commit_message TEXT,
            branch TEXT,
            branch_base TEXT,
            image TEXT,
            container_name TEXT,
            exit_code INTEGER,
            lines_added INTEGER,
            lines_removed INTEGER,
            files_changed INTEGER,
            score_tooling INTEGER,
            score_clarity INTEGER,
            score_confidence INTEGER,
            time_research_pct INTEGER,
            time_coding_pct INTEGER,
            time_debugging_pct INTEGER,
            time_tooling_pct INTEGER,
            discovered_beads_count INTEGER,
            has_experience_report INTEGER,
            output_dir TEXT,
            last_snippet TEXT,
            token_count INTEGER,
            cpu_pct REAL,
            cpu_usec INTEGER,
            mem_mb INTEGER,
            last_activity DATETIME,
            jsonl_offset INTEGER,
            tool_count INTEGER,
            turn_count INTEGER,
            librarian_type TEXT,
            failure_class TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def insert_dispatch_run(
    db_path: Path, *, run_id: str, bead_id: str,
    status: str = "RUNNING",
    started_at: str | None = None,
    last_activity=None,
    output_dir: str | None = None,
    container_name: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    if started_at is None:
        from datetime import datetime, timezone
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO dispatch_runs"
        " (id, bead_id, started_at, status, output_dir, container_name, last_activity, branch)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, bead_id, started_at, status, output_dir, container_name,
         last_activity, f"agent/{bead_id}"),
    )
    conn.commit()
    conn.close()


def insert_tmux_session(
    db_path: Path, *, tmux_name: str, type_: str,
    jsonl_path: str | None = None,
    is_live: int = 1,
    bead_id: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    res_dir = str(Path(jsonl_path).parent) if jsonl_path else None
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, bead_id, jsonl_path, resolution_dir,"
        "  curr_jsonl_file, created_at, is_live)"
        " VALUES (?, ?, 'autonomy', ?, ?, ?, ?, ?, ?)",
        (tmux_name, type_, bead_id, jsonl_path, res_dir, jsonl_path,
         time.time(), is_live),
    )
    conn.commit()
    conn.close()


def fetch_dispatch_row(db_path: Path, run_id: str) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatch_runs WHERE id=?", (run_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def fetch_tmux_row(db_path: Path, tmux_name: str) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def write_decision_json(output_dir: Path, *, status: str, reason: str = "") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    decision_path = output_dir / "decision.json"
    decision_path.write_text(json.dumps({
        "status": status,
        "reason": reason,
        "artifacts": [],
        "notes": "",
        "scores": {"tooling": 5, "clarity": 5, "confidence": 5},
    }))
    return decision_path


# ── Combined env fixture ─────────────────────────────────────────────


@pytest.fixture
def cleanup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Isolate both DBs + agent-runs directory, reload the affected modules."""
    dashboard_db = tmp_path / "dashboard.db"
    dispatch_db = tmp_path / "dispatch.db"
    agent_runs = tmp_path / "agent-runs"
    agent_runs.mkdir(parents=True)

    init_dashboard_db(dashboard_db)
    init_dispatch_db(dispatch_db)

    monkeypatch.setenv("DASHBOARD_DB", str(dashboard_db))
    monkeypatch.setenv("DISPATCH_DB", str(dispatch_db))
    monkeypatch.setenv("DASHBOARD_AGENT_RUNS_DIR", str(agent_runs))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    import importlib
    from tools.dashboard.dao import dashboard_db as dbmod
    importlib.reload(dbmod)
    from tools.dashboard.dao import dispatch as dispatch_dao
    importlib.reload(dispatch_dao)
    from agents import dispatch_db as dispatcher_dbmod
    importlib.reload(dispatcher_dbmod)
    from tools.dashboard import session_monitor as smmod
    importlib.reload(smmod)
    from tools.dashboard import server as srvmod
    importlib.reload(srvmod)
    from agents import dispatcher as dispatcher_mod
    importlib.reload(dispatcher_mod)

    yield {
        "tmp_path": tmp_path,
        "dashboard_db": dashboard_db,
        "dispatch_db": dispatch_db,
        "agent_runs": agent_runs,
        "dashboard_db_mod": dbmod,
        "dispatch_dao": dispatch_dao,
        "dispatcher_db_mod": dispatcher_dbmod,
        "session_monitor": smmod,
        "server": srvmod,
        "dispatcher": dispatcher_mod,
    }
