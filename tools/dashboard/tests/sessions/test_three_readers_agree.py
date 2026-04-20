"""
Phase 0 — Three-reader agreement: L3 integration test.

Bead auto-ylj6r. This is the canonical test for the three-readers problem
(graph://554a08c6-887). The three JSONL readers — session_monitor state,
dispatcher's card stats reader, and the tail API — must agree on the
entry count for a running dispatch at the same instant. MUST FAIL on
master.

Covered test from the bead's Phase 0 test table:
  #28 test_three_readers_agree
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


def _append_entry(jsonl: Path, text: str) -> None:
    with open(jsonl, "a") as f:
        f.write(json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
            "timestamp": "2026-04-20T00:00:00Z",
        }) + "\n")


class TestThreeReadersAgree:
    """#28 — Monitor state, card stats source, tail endpoint all report
    the same entry_count for a running dispatch.

    FAILS TODAY: three independent readers (session_monitor tailer,
    dispatcher._read_jsonl_incremental, api_dispatch_tail) each maintain
    their own byte-offset and run at different cadences. At a fixed moment
    they disagree — the bug graph://554a08c6-887 documents exactly this.
    After consolidation, only the monitor reads; the other two read from
    the monitor's shared state (or DB). Entry counts must match.

    Acceptance: write N entries, then poll each reader. All three MUST
    converge on the same count within a small window (3s). The assertion
    is a strict equality across the three.
    """

    @pytest.mark.asyncio
    async def test_three_readers_agree(self, tmp_path):
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
            # Set up a running dispatch: JSONL + DB row + register with monitor
            bead_id = "auto-threeread"
            disp_tmux = f"{bead_id}-0420-040404"
            disp_uuid = "40404040-1111-2222-3333-444444444444"
            run_dir = tmp_path / "agent-runs" / disp_tmux
            proj_dir = run_dir / "sessions" / "autonomy"
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

            # Boot the monitor so inotify tails the JSONL
            bus = server_mod.event_bus if hasattr(server_mod, "event_bus") else None
            mon = sm_mod.SessionMonitor()
            with patch.object(sm_mod.SessionMonitor, "_check_tmux",
                              staticmethod(lambda name: True)):
                await mon.start(event_bus=bus,
                                entry_parser=server_mod._parse_jsonl_entry)

                # Write N entries
                N = 5
                for i in range(N):
                    _append_entry(jsonl, f"entry {i}")
                # Give monitor time to catch up
                await asyncio.sleep(2)

                # Reader 1 — monitor's shared state.
                # The unified API is expected to be `get_session_stats`
                # (per bead Phase 1: "card stats read from
                # monitor.get_session_stats(session_id)").
                assert hasattr(mon, "get_session_stats"), (
                    "SessionMonitor.get_session_stats missing — Phase 1/2 "
                    "API not yet added. Card stats must read through the "
                    "monitor (single source of truth)."
                )
                monitor_stats = mon.get_session_stats(disp_tmux)
                assert isinstance(monitor_stats, dict) and "entry_count" in monitor_stats, (
                    f"get_session_stats should return a dict with 'entry_count'; "
                    f"got {monitor_stats!r}"
                )
                monitor_count = int(monitor_stats["entry_count"])

                # Reader 2 — dispatcher's reader MUST be gone.
                # The assertion enforces that the duplicate reader is
                # removed; if it still exists, three-readers isn't fixed.
                from agents import dispatcher
                assert not hasattr(dispatcher, "_read_jsonl_incremental"), (
                    "agents.dispatcher._read_jsonl_incremental still exists — "
                    "three-readers problem is not resolved. Phase 2 deletes "
                    "this function and routes card stats through monitor."
                )

                # Reader 3 — tail endpoint (session unified tail).
                from starlette.testclient import TestClient
                with TestClient(server_mod.app) as client:
                    resp = client.get(
                        f"/api/session/autonomy/{disp_tmux}/tail"
                    )
                    assert resp.status_code == 200, (
                        f"Unified tail returned {resp.status_code} "
                        f"for running dispatch; body={resp.text[:200]!r}"
                    )
                    tail_data = resp.json()
                    tail_count = len(tail_data.get("entries") or [])

                # All three must agree.
                assert monitor_count == tail_count == N, (
                    f"Three readers disagree: monitor={monitor_count}, "
                    f"tail={tail_count}, expected={N}. "
                    "The three-reader consolidation has not landed — "
                    "each reader maintains its own view of JSONL state."
                )

                await mon.stop()
        finally:
            os.environ.pop("DASHBOARD_DB", None)
