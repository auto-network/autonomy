"""
Phase 0 — Unified tail server contract: L1 server tests.

Bead auto-ylj6r. The unified `/api/session/{proj}/{id}/tail` endpoint must
resolve id across all session types (tmux_name, dispatch session_uuid,
run_dir-derived id). These tests MUST FAIL on master — Phase 3 delivers
the generalisation that makes them pass.

Covered tests from the bead's Phase 0 test table:
  #9  test_unified_tail_resolves_dispatch_uuid
  #10 test_ended_dispatch_resolves_by_uuid
  #11 test_pruned_session_returns_404
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


def _insert_dispatch(db_path: Path, *, tmux_name: str, session_uuid: str,
                     jsonl_path: str, is_live: int = 1, bead_id: str = "auto-t") -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, bead_id, jsonl_path, session_uuid,"
        "  resolution_dir, session_uuids, curr_jsonl_file, created_at, is_live)"
        " VALUES (?, 'dispatch', 'autonomy', ?, ?, ?, ?, ?, ?, ?, ?)",
        (tmux_name, bead_id, jsonl_path, session_uuid,
         str(Path(jsonl_path).parent), json.dumps([session_uuid]),
         jsonl_path, time.time(), is_live),
    )
    conn.commit()
    conn.close()


def _write_entry(jsonl: Path, text: str) -> None:
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


@pytest.fixture
def unified_tail_client(tmp_path):
    """Sync TestClient backed by an isolated dashboard.db (no mock DAO).

    We want the REAL tail endpoint code path — not the DASHBOARD_MOCK
    shortcut in api_session_tail — so this fixture sets DASHBOARD_DB
    to an in-tree sqlite file and reloads the server.
    """
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


# ── Test 9 — running dispatch resolves by UUID ─────────────────────────


class TestUnifiedTailResolvesDispatchUUID:
    """#9 — GET /api/session/{proj}/{dispatch_uuid}/tail returns entries.

    FAILS TODAY: api_session_tail looks up the id via session_monitor.get_one
    (which queries by tmux_name PRIMARY KEY). A dispatch session_uuid is
    stored in the session_uuid column, not the primary key, so a UUID lookup
    hits the `db_row is None` branch and returns 400 "Invalid project or
    session_id". Phase 3 generalises resolution to try tmux_name, then
    session_uuid, then session_uuids (JSON array), then run_dir fallback.
    """

    def test_unified_tail_resolves_dispatch_uuid(self, unified_tail_client):
        client, tmp_path, db_path, _server = unified_tail_client

        proj_dir = tmp_path / "agent-runs" / "auto-disp-0420-010101" / "sessions" / "autonomy"
        proj_dir.mkdir(parents=True)
        dispatch_uuid = "11111111-2222-3333-4444-555555555555"
        jsonl = proj_dir / f"{dispatch_uuid}.jsonl"
        _write_entry(jsonl, "running dispatch entry")

        _insert_dispatch(
            db_path,
            tmux_name="auto-disp-0420-010101",
            session_uuid=dispatch_uuid,
            jsonl_path=str(jsonl),
            is_live=1,
        )

        resp = client.get(f"/api/session/autonomy/{dispatch_uuid}/tail")
        assert resp.status_code == 200, (
            f"Expected 200 resolving dispatch by session_uuid; got "
            f"{resp.status_code} body={resp.text[:200]!r}"
        )
        data = resp.json()
        assert "entries" in data, f"no entries field: {data!r}"
        assert len(data["entries"]) >= 1, (
            f"Expected at least one entry for running dispatch; got {data!r}"
        )
        assert data.get("is_live") is True, (
            f"Expected is_live=True for running dispatch; got {data!r}"
        )


# ── Test 10 — ended dispatch resolves by UUID ──────────────────────────


class TestEndedDispatchResolvesByUUID:
    """#10 — Ended dispatch: same endpoint returns entries + is_live:false.

    FAILS TODAY: Same resolution gap as #9 — dispatch UUIDs don't resolve
    through api_session_tail. Additionally, historical rows don't exist
    yet (backfill is Phase 1), so even if resolution worked the ended
    dispatch would not be found in tmux_sessions.
    """

    def test_ended_dispatch_resolves_by_uuid(self, unified_tail_client):
        client, tmp_path, db_path, _server = unified_tail_client

        proj_dir = tmp_path / "agent-runs" / "auto-ended-0420-020202" / "sessions" / "autonomy"
        proj_dir.mkdir(parents=True)
        ended_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        jsonl = proj_dir / f"{ended_uuid}.jsonl"
        _write_entry(jsonl, "ended dispatch last entry")

        _insert_dispatch(
            db_path,
            tmux_name="auto-ended-0420-020202",
            session_uuid=ended_uuid,
            jsonl_path=str(jsonl),
            is_live=0,
        )

        resp = client.get(f"/api/session/autonomy/{ended_uuid}/tail")
        assert resp.status_code == 200, (
            f"Ended dispatch must resolve by uuid; got {resp.status_code} "
            f"body={resp.text[:200]!r}"
        )
        data = resp.json()
        assert len(data.get("entries", [])) >= 1, (
            f"Expected entries for ended dispatch; got {data!r}"
        )
        assert data.get("is_live") is False, (
            f"Expected is_live=False for ended dispatch; got {data!r}"
        )


# ── Test 11 — pruned session returns 404 with structured error ────────


class TestPrunedSessionReturns404:
    """#11 — Session not in tmux_sessions + JSONL not on disk → 404 {error}.

    FAILS TODAY: The current api_session_tail returns 400 "Invalid project
    or session_id" when db_row is None — NOT 404. The unified contract
    requires 404 for definite-miss ("session not found") distinct from
    400 (malformed input). Phase 3 makes this separation explicit.
    """

    def test_pruned_session_returns_404(self, unified_tail_client):
        client, *_ = unified_tail_client

        resp = client.get(
            "/api/session/autonomy/nonexistent-99999999-0000-0000-0000-000000000000/tail"
        )
        assert resp.status_code == 404, (
            f"Expected 404 for pruned/unknown session id; got {resp.status_code} "
            f"body={resp.text[:200]!r}. Current master returns 400 which is "
            "incorrect — 400 means malformed request, 404 means not found."
        )
        data = resp.json()
        assert "error" in data, (
            f"404 response must include structured `error` field; got {data!r}"
        )
