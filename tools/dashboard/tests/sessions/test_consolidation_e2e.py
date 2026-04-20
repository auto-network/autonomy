"""
Phase 0 — Dispatch end-to-end live updates: L3 integration test.

Bead auto-ylj6r. Simulates a running dispatch by writing JSONL entries
over time and asserting that the unified tail endpoint reflects the new
entries on successive polls. MUST FAIL on master.

Covered test from the bead's Phase 0 test table:
  #27 test_dispatch_end_to_end_live_updates
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_asyncio")


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


def _append_entry(jsonl: Path, i: int) -> None:
    with open(jsonl, "a") as f:
        f.write(json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"step {i}"}],
            },
            "timestamp": "2026-04-20T00:00:00Z",
        }) + "\n")


class TestDispatchEndToEndLiveUpdates:
    """#27 — Live dispatch: tail entry_count grows across successive polls.

    FAILS TODAY:
      1. api_session_tail cannot resolve a dispatch UUID (tmux_name PK only).
      2. Even if we use the dispatch tmux_name as the id, master's tail
         endpoint reads the file directly without using monitor state;
         that's incidentally OK for L3 but fails when combined with
         dispatch's own _read_jsonl_incremental which duplicates offset
         tracking (three-readers problem).
    Phase 3 + Phase 2 together make this test pass.
    """

    @pytest.mark.asyncio
    async def test_dispatch_end_to_end_live_updates(self, tmp_path):
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

        try:
            # Dispatch fixture
            bead_id = "auto-e2e"
            disp_tmux = f"{bead_id}-0420-050505"
            disp_uuid = "50505050-aaaa-bbbb-cccc-000000000005"
            proj_dir = tmp_path / "agent-runs" / disp_tmux / "sessions" / "autonomy"
            proj_dir.mkdir(parents=True)
            jsonl = proj_dir / f"{disp_uuid}.jsonl"
            jsonl.write_text("")

            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "INSERT INTO tmux_sessions"
                " (tmux_name, type, project, bead_id, jsonl_path, session_uuid,"
                "  resolution_dir, session_uuids, curr_jsonl_file, created_at, is_live)"
                " VALUES (?, 'dispatch', 'autonomy', ?, ?, ?, ?, ?, ?, ?, 1)",
                (disp_tmux, bead_id, str(jsonl), disp_uuid,
                 str(proj_dir), json.dumps([disp_uuid]),
                 str(jsonl), time.time()),
            )
            conn.commit()
            conn.close()

            mon = sm_mod.SessionMonitor()
            with patch.object(sm_mod.SessionMonitor, "_check_tmux",
                              staticmethod(lambda name: True)):
                await mon.start(
                    event_bus=None,
                    entry_parser=server_mod._parse_jsonl_entry,
                )

                from starlette.testclient import TestClient
                with TestClient(server_mod.app) as client:
                    # Poll via the unified tail, using the DISPATCH UUID as id
                    # (the Recent-card route uses the uuid, so that's the
                    # realistic failure path).
                    counts = []
                    for step in range(4):
                        _append_entry(jsonl, step)
                        await asyncio.sleep(1.2)
                        resp = client.get(
                            f"/api/session/autonomy/{disp_uuid}/tail"
                        )
                        assert resp.status_code == 200, (
                            f"Unified tail step={step}: status="
                            f"{resp.status_code} body={resp.text[:200]!r}. "
                            "Tail endpoint cannot resolve dispatch UUID."
                        )
                        data = resp.json()
                        # After step, total entries should be step+1
                        count = len(data.get("entries") or [])
                        counts.append(count)

                    # Must be strictly monotonic increasing
                    assert counts == sorted(counts) and len(set(counts)) > 1, (
                        f"Tail counts did not grow across polls: {counts!r}. "
                        "Live-update contract broken — tail is not reading "
                        "the running JSONL."
                    )
                    assert counts[-1] >= 4, (
                        f"Expected ≥4 entries after 4 writes; got {counts[-1]} "
                        f"(counts={counts!r}). Offset tracking is wrong."
                    )

                await mon.stop()
        finally:
            os.environ.pop("DASHBOARD_DB", None)
