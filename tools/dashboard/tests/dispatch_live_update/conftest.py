"""Fixtures for auto-yaw58 — dispatch live-update session_id seam tests.

Crucial: NONE of these helpers auto-populate a `session_id` field on inserted
rows. `tmux_name` and `session_uuid` are always set as distinct columns, and
we assert which one real code paths return. That's what makes these tests
catch the seam bug the auto-hy3pl item 4 mock tests hid.
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


# ── dashboard.db schema ──────────────────────────────────────────────


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


def init_dispatch_db(db_path: Path) -> None:
    """Touch an empty dispatch.db file. The server's init_db() creates the
    full schema on first open; we don't pre-create any tables to avoid
    schema drift (e.g. missing columns that CREATE INDEX references).
    """
    db_path.touch()


def insert_raw_dispatch_row(
    db_path: Path,
    *,
    tmux_name: str,
    session_uuid: str,
    jsonl_path: str,
    bead_id: str | None = None,
) -> None:
    """Plant a dispatch row with tmux_name and session_uuid as distinct values.

    Deliberately does NOT set any 'session_id' field — it's not a column.
    The real DAO decides which of (tmux_name, session_uuid) gets mapped to
    the response's session_id. That's what we're testing.
    """
    assert tmux_name != session_uuid, (
        "Test invariant: tmux_name and session_uuid must be distinct — the "
        "whole point is to see which one the real code returns."
    )
    conn = sqlite3.connect(str(db_path))
    res_dir = str(Path(jsonl_path).parent)
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, bead_id, jsonl_path, session_uuid,"
        "  resolution_dir, session_uuids, curr_jsonl_file,"
        "  created_at, is_live)"
        " VALUES (?, 'dispatch', 'autonomy', ?, ?, ?, ?, ?, ?, ?, 1)",
        (tmux_name, bead_id, jsonl_path, session_uuid,
         res_dir, json.dumps([session_uuid]), jsonl_path, time.time()),
    )
    conn.commit()
    conn.close()


def append_jsonl(jsonl: Path, text: str) -> None:
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": "2026-04-20T00:00:00Z",
    }
    with open(jsonl, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── Combined environment fixture ─────────────────────────────────────


@pytest.fixture
def seam_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Isolated DBs + agent-runs + reloaded modules. No mock DAO."""
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
    from tools.dashboard.dao import sessions as sessmod
    importlib.reload(sessmod)
    from tools.dashboard import session_monitor as smmod
    importlib.reload(smmod)
    from tools.dashboard import server as srvmod
    importlib.reload(srvmod)

    # server.AGENT_RUNS_DIR is a module-level constant resolved from file
    # layout at import time, not from an env var. Override it so
    # _find_session_files scans our test agent-runs tree, not the repo's
    # production data/agent-runs directory.
    srvmod.AGENT_RUNS_DIR = agent_runs

    yield {
        "tmp_path": tmp_path,
        "dashboard_db": dashboard_db,
        "dispatch_db": dispatch_db,
        "agent_runs": agent_runs,
        "dashboard_db_mod": dbmod,
        "sessions_dao": sessmod,
        "session_monitor": smmod,
        "server": srvmod,
    }
