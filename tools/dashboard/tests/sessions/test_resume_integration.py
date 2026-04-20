"""
Phase 0 — Resume integration: L3 integration tests.

Bead auto-ylj6r. Exercises the resume flow where a dead dispatch session
is resumed by the user, spawning a new interactive container session that
appears on the Active list. Also validates that a LIVE dispatch session
is still hidden from the Active list. Both MUST FAIL on master.

Covered tests from the bead's Phase 0 test table:
  #25 test_resume_dead_dispatch_spawns_interactive_in_active
  #26 test_running_dispatch_absent_from_active

These are L3: real HTTP + real Starlette TestClient + real
dashboard.db. They don't spawn actual tmux sessions — they simulate
the POST /api/dispatch/resume flow and assert the resulting registry
shape. The resume endpoint's contract: a new row with type='container'
and session_uuid different from the original dispatch row must be
created (via session_monitor.register_session), and it must surface on
GET /api/dao/active_sessions.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


def _init_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS tmux_sessions (
        tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
        type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
        bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,
        file_offset INTEGER DEFAULT 0, last_activity REAL,
        last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
        context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
        topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
        nag_enabled INTEGER DEFAULT 0, nag_interval INTEGER DEFAULT 15,
        nag_message TEXT DEFAULT '', nag_last_sent REAL DEFAULT 0,
        dispatch_nag INTEGER DEFAULT 0,
        resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
        curr_jsonl_file TEXT, activity_state TEXT DEFAULT 'idle'
    )""")
    conn.commit()
    conn.close()


def _insert(db_path: Path, *, tmux_name: str, type_: str, session_uuid: str,
            jsonl_path: str | None = None, is_live: int = 1,
            bead_id: str | None = None) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, bead_id, jsonl_path, session_uuid,"
        "  resolution_dir, session_uuids, curr_jsonl_file, created_at, is_live)"
        " VALUES (?, ?, 'autonomy', ?, ?, ?, ?, ?, ?, ?, ?)",
        (tmux_name, type_, bead_id, jsonl_path, session_uuid,
         str(Path(jsonl_path).parent) if jsonl_path else None,
         json.dumps([session_uuid]) if session_uuid else "[]",
         jsonl_path, time.time(), is_live),
    )
    conn.commit()
    conn.close()


def _live_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM tmux_sessions WHERE is_live=1"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@pytest.fixture
def live_client(tmp_path):
    """Sync TestClient backed by an isolated dashboard.db."""
    db_path = tmp_path / "dashboard.db"
    _init_db(db_path)

    os.environ.pop("DASHBOARD_MOCK", None)
    os.environ["DASHBOARD_DB"] = str(db_path)

    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from tools.dashboard import session_monitor as sm_mod
    importlib.reload(sm_mod)
    from tools.dashboard import server as server_mod
    importlib.reload(server_mod)

    from starlette.testclient import TestClient
    with TestClient(server_mod.app) as client:
        yield client, tmp_path, db_path, server_mod

    os.environ.pop("DASHBOARD_DB", None)


def _poll_active(client, predicate, timeout=30, interval=1.0) -> dict | None:
    """Poll /api/dao/active_sessions until `predicate(rows)` is truthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/api/dao/active_sessions")
        if resp.status_code == 200:
            rows = resp.json()
            matched = predicate(rows)
            if matched:
                return matched
        time.sleep(interval)
    return None


# ── Test 25 — resume dead dispatch spawns interactive ─────────────────


class TestResumeDeadDispatchSpawnsInteractive:
    """#25 — Resume dead dispatch → new container session in Active list.

    FAILS TODAY:
      1. There is no POST /api/dispatch/resume endpoint that creates a new
         interactive (container) session from a dead dispatch row. Phase 5
         (or an adjacent resume feature) must provide this.
      2. Even if a new row is created, get_active_sessions() currently
         returns ALL is_live=1 rows without type-filtering. Phase 5 adds
         the type filter which makes the test both meaningful AND the
         assertion about session_uuid differing from the dispatch's
         reachable.

    Acceptance shape:
      - client.post(f"/api/dispatch/resume/{bead_id}") → 200
      - Within 30s: /api/dao/active_sessions contains a row with
        type='container' and session_uuid != original dispatch's uuid.
    """

    def test_resume_dead_dispatch_spawns_interactive_in_active(self, live_client):
        client, tmp_path, db_path, _server = live_client

        # Plant a dead dispatch row to resume
        disp_tmux = "auto-resume-0420-010101"
        disp_uuid = "01010101-aaaa-bbbb-cccc-000000000001"
        run_sess_dir = tmp_path / "agent-runs" / disp_tmux / "sessions" / "autonomy"
        run_sess_dir.mkdir(parents=True)
        disp_jsonl = run_sess_dir / f"{disp_uuid}.jsonl"
        disp_jsonl.write_text(
            json.dumps({"type": "system", "content": "ok"}) + "\n"
        )
        _insert(
            db_path,
            tmux_name=disp_tmux,
            type_="dispatch",
            session_uuid=disp_uuid,
            jsonl_path=str(disp_jsonl),
            is_live=0,
            bead_id="auto-resume",
        )

        # POST the resume endpoint
        resp = client.post(f"/api/dispatch/resume/auto-resume")
        assert resp.status_code in (200, 201, 202), (
            f"POST /api/dispatch/resume/auto-resume returned {resp.status_code}; "
            f"body={resp.text[:200]!r}. Endpoint is missing on master."
        )

        # Wait for the new interactive session to appear on Active list.
        # A new container row, session_uuid differing from original dispatch.
        def _predicate(rows):
            for s in rows:
                stype = s.get("type")
                sid = s.get("session_id") or s.get("tmux_session") or ""
                s_uuid = s.get("session_uuid") or ""
                if stype == "container" and s_uuid != disp_uuid and s_uuid:
                    return s
                if stype == "container" and sid and sid != disp_tmux:
                    return s
            return None

        matched = _poll_active(client, _predicate, timeout=30, interval=1.0)
        assert matched is not None, (
            "No new interactive (container) session appeared on /api/dao/active_sessions "
            "within 30s after resume. /api/dispatch/resume must spawn an "
            "interactive container and register it with session_monitor "
            "(type='container') whose session_uuid differs from the original."
        )

        # Dead dispatch row must NOT have been revived into is_live=1 —
        # it stays dead. Resume creates a NEW row.
        live_types = {r["type"] for r in _live_rows(db_path)}
        assert "container" in live_types, (
            f"Resume produced no live container row in DB; live types={live_types!r}"
        )


# ── Test 26 — running dispatch absent from Active list ────────────────


class TestRunningDispatchAbsentFromActive:
    """#26 — Live dispatch in monitor registry → still not on Active list.

    FAILS TODAY: get_active_sessions() returns type IN {container, host}
    today only because the monitor never registers dispatch sessions — so
    the fact that dispatch is excluded is a side-effect, not a filter.
    Phase 1 registers dispatch with type='dispatch' (making it present in
    the registry) and Phase 5 explicitly filters it out. This test pins
    the contract: a dispatch row in the registry must NOT appear on the
    /api/dao/active_sessions response.
    """

    def test_running_dispatch_absent_from_active(self, live_client):
        client, tmp_path, db_path, _server = live_client

        disp_tmux = "auto-running-disp-0420-030303"
        disp_uuid = "30303030-aaaa-bbbb-cccc-000000000003"
        sess_dir = tmp_path / "agent-runs" / disp_tmux / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        disp_jsonl = sess_dir / f"{disp_uuid}.jsonl"
        disp_jsonl.write_text(
            json.dumps({"type": "system", "content": "running"}) + "\n"
        )

        # Register a LIVE dispatch row directly via DB (simulates monitor state)
        _insert(
            db_path,
            tmux_name=disp_tmux,
            type_="dispatch",
            session_uuid=disp_uuid,
            jsonl_path=str(disp_jsonl),
            is_live=1,
            bead_id="auto-running",
        )

        # Active list must NOT include it
        resp = client.get("/api/dao/active_sessions")
        assert resp.status_code == 200
        rows = resp.json()

        for s in rows:
            stype = s.get("type")
            sid = s.get("session_id") or s.get("tmux_session") or ""
            assert stype != "dispatch", (
                f"Dispatch row {sid!r} found in Active list with type='dispatch'. "
                "Phase 5 filter not in place. Registry must include dispatch "
                "but /api/dao/active_sessions must exclude it."
            )
            assert sid != disp_tmux, (
                f"Live dispatch tmux {disp_tmux!r} leaked into Active list: "
                f"row={s!r}"
            )
