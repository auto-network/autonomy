"""Shared fixtures for auto-opbyh — dispatcher↔monitor IPC contract tests.

All tests here drive the REAL in-process SessionMonitor through either the
HTTP endpoints (tests 1, 2, 3, 5) or a direct _liveness_loop tick (test 4).
No patching of _check_tmux, no direct in-process calls to register_session.
These tests exist specifically to catch gaps that the auto-ylj6r Phase 0
contract missed by over-mocking.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("pytest_asyncio")


# ── DB helpers ────────────────────────────────────────────────────────


def _init_dashboard_db(db_path: Path) -> None:
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


def fetch_row(db_path: Path, tmux_name: str) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def insert_row(
    db_path: Path,
    *,
    tmux_name: str,
    type_: str,
    jsonl_path: str | None = None,
    is_live: int = 1,
    bead_id: str | None = None,
    session_uuid: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    res_dir = str(Path(jsonl_path).parent) if jsonl_path else None
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, bead_id, jsonl_path, session_uuid,"
        "  resolution_dir, session_uuids, curr_jsonl_file, created_at, is_live)"
        " VALUES (?, ?, 'autonomy', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tmux_name, type_, bead_id, jsonl_path, session_uuid,
            res_dir, json.dumps([session_uuid]) if session_uuid else "[]",
            jsonl_path, time.time(), is_live,
        ),
    )
    conn.commit()
    conn.close()


# ── Environment fixture: isolated DBs + reloaded modules ─────────────


@pytest.fixture
def ipc_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Set DASHBOARD_DB + DISPATCH_DB to tmp paths, reload the affected modules.

    Returns a dict with:
      tmp_path       — fixture tmp dir
      db_path        — the dashboard.db
      dispatch_db    — the dispatch.db
      dashboard_db   — the reloaded dashboard_db module
      session_monitor — the reloaded session_monitor module
      server         — the reloaded server module (app ready to wrap in TestClient)
    """
    db_path = tmp_path / "dashboard.db"
    _init_dashboard_db(db_path)
    dispatch_db = tmp_path / "dispatch.db"

    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.setenv("DISPATCH_DB", str(dispatch_db))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    import importlib
    from tools.dashboard.dao import dashboard_db as dbmod
    importlib.reload(dbmod)
    from tools.dashboard import session_monitor as smmod
    importlib.reload(smmod)
    from tools.dashboard import server as srvmod
    importlib.reload(srvmod)

    yield {
        "tmp_path": tmp_path,
        "db_path": db_path,
        "dispatch_db": dispatch_db,
        "dashboard_db": dbmod,
        "session_monitor": smmod,
        "server": srvmod,
    }


