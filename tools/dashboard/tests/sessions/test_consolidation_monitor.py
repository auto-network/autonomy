"""
Phase 0 — Session monitor consolidation: L1 unit tests.

Bead auto-ylj6r. Tests for monitor registry extension to cover all session
types (container/host/dispatch/librarian). These MUST FAIL on master —
they encode the contract that Phase 1 + Phase 2 implementation must satisfy.

Covered tests from the bead's Phase 0 test table:
  #1 test_monitor_registers_dispatch_session
  #2 test_monitor_broadcasts_sse_for_dispatch
  #3 test_monitor_deregisters_on_decision_json
  #7 test_historical_dispatch_session_backfilled
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_asyncio")


# ── Helpers ────────────────────────────────────────────────────────────


def _init_test_db(db_path: Path) -> None:
    """Create a minimal dashboard.db matching the production schema."""
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


@pytest.fixture
def monitor_env(tmp_path):
    """Point the session_monitor module at an isolated dashboard.db."""
    db_path = tmp_path / "dashboard.db"
    _init_test_db(db_path)
    os.environ["DASHBOARD_DB"] = str(db_path)

    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)

    # Also reload session_monitor to bind to the reloaded dashboard_db
    from tools.dashboard import session_monitor as sm_mod
    importlib.reload(sm_mod)

    yield tmp_path, db_path, sm_mod

    os.environ.pop("DASHBOARD_DB", None)


def _fetch_row(db_path: Path, tmux_name: str) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Test 1 — register_session accepts type='dispatch' ─────────────────


class TestMonitorRegistersDispatch:
    """#1 — register_session(type='dispatch', ...) must persist a row.

    FAILS TODAY: Phase 1 adds a new public method `register_session` on
    SessionMonitor with the signature:
        register_session(tmux_name, type, jsonl_path, run_dir=None, bead_id=None)
    that accepts type in {'container','host','dispatch','librarian'}. The
    current API is `register(tmux_name, session_type, project, ...)`; no
    caller sets type='dispatch', and the dispatcher never touches the
    monitor at all. On master this test fails because `register_session`
    does not exist as a method on SessionMonitor.
    """

    @pytest.mark.asyncio
    async def test_monitor_registers_dispatch_session(self, monitor_env):
        """register_session(type='dispatch') writes a row with type='dispatch'."""
        tmp_path, db_path, sm_mod = monitor_env
        jsonl = tmp_path / "agent-runs" / "auto-xyz-001" / "sessions" / "run.jsonl"
        jsonl.parent.mkdir(parents=True)
        jsonl.write_text("")

        mon = sm_mod.SessionMonitor()

        assert hasattr(mon, "register_session"), (
            "SessionMonitor.register_session missing — Phase 1 API not yet added. "
            "Expected: register_session(tmux_name, type, jsonl_path, run_dir=None, bead_id=None)"
        )
        sig = inspect.signature(mon.register_session)
        assert "type" in sig.parameters, (
            "register_session signature must include a `type` parameter; "
            f"got {list(sig.parameters)}"
        )

        await mon.register_session(
            tmux_name="auto-xyz-dispatch",
            type="dispatch",
            jsonl_path=jsonl,
            run_dir=jsonl.parent.parent,
            bead_id="auto-xyz",
        )

        row = _fetch_row(db_path, "auto-xyz-dispatch")
        assert row is not None, "register_session did not persist a row"
        assert row["type"] == "dispatch", (
            f"persisted row type={row['type']!r}, expected 'dispatch'"
        )
        assert row["jsonl_path"] == str(jsonl), (
            f"jsonl_path not persisted: {row['jsonl_path']!r}"
        )


# ── Test 2 — dispatch session triggers SSE session:messages ───────────


class TestMonitorBroadcastsSSEForDispatch:
    """#2 — After registering dispatch + writing JSONL, session:messages fires.

    FAILS TODAY: Registration of dispatch sessions is not yet wired, so
    no inotify watch is installed, so writes to the dispatch JSONL don't
    produce session:messages events. The dispatcher has its own stats
    reader (_read_jsonl_incremental) that bypasses the monitor entirely.
    """

    @pytest.mark.asyncio
    async def test_monitor_broadcasts_sse_for_dispatch(self, monitor_env):
        """Writing to a registered dispatch JSONL produces session:messages."""
        tmp_path, db_path, sm_mod = monitor_env
        from tools.dashboard.event_bus import EventBus
        from tools.dashboard.server import _parse_jsonl_entry

        sess_dir = tmp_path / "agent-runs" / "auto-disp-002" / "sessions"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / "disp.jsonl"
        jsonl.write_text("")

        bus = EventBus()
        mon = sm_mod.SessionMonitor()

        assert hasattr(mon, "register_session"), (
            "register_session missing — Phase 1 API not yet added."
        )

        with patch.object(sm_mod.SessionMonitor, "_check_tmux",
                          staticmethod(lambda name: True)):
            await mon.start(event_bus=bus, entry_parser=_parse_jsonl_entry)
            await mon.register_session(
                tmux_name="auto-disp-session",
                type="dispatch",
                jsonl_path=jsonl,
                run_dir=sess_dir.parent,
                bead_id="auto-disp",
            )

            # Drain registry events we don't care about
            q = bus.subscribe()
            await asyncio.sleep(0.1)
            while not q.empty():
                q.get_nowait()

            # Append an entry — must trigger session:messages on dispatch type
            with open(jsonl, "a") as f:
                f.write(json.dumps({
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "hello from dispatch"}],
                    },
                    "timestamp": "2026-04-20T00:00:00Z",
                }) + "\n")

            deadline = time.monotonic() + 3.0
            got_messages = False
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    topic, data, _seq = await asyncio.wait_for(
                        q.get(), timeout=max(0.01, remaining)
                    )
                except asyncio.TimeoutError:
                    break
                if topic == "session:messages" and data.get("session_id") == "auto-disp-session":
                    got_messages = True
                    break

            await mon.stop()

            assert got_messages, (
                "Expected session:messages broadcast for dispatch session after "
                "JSONL write; none received within 3s. Monitor is not yet "
                "tailing dispatch JSONLs."
            )


# ── Test 3 — deregister marks is_live=0 but preserves jsonl_path ──────


class TestMonitorDeregisterKeepsRow:
    """#3 — Deregister marks row is_live=0 but keeps row with jsonl_path.

    FAILS TODAY: Dispatch registration isn't wired; also, the
    deregister-triggered-by-decision.json hook on dispatcher side does
    not exist. The assertion that a row exists with type='dispatch' and
    is_live=0 AFTER decision.json cannot currently be produced by any
    dispatcher code path. This test exercises the monitor API directly:
    register_session(type='dispatch') + deregister_session(tmux) must
    leave a row with is_live=0 and jsonl_path preserved.
    """

    @pytest.mark.asyncio
    async def test_monitor_deregisters_on_decision_json(self, monitor_env):
        """Deregister preserves the row with jsonl_path and sets is_live=0."""
        tmp_path, db_path, sm_mod = monitor_env
        sess_dir = tmp_path / "agent-runs" / "auto-disp-003" / "sessions"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / "disp.jsonl"
        jsonl.write_text("")

        mon = sm_mod.SessionMonitor()

        assert hasattr(mon, "register_session"), (
            "register_session missing — Phase 1 API not yet added."
        )
        assert hasattr(mon, "deregister_session"), (
            "deregister_session missing — Phase 1 API not yet added. "
            "Expected: deregister_session(tmux_name) marks is_live=0 without "
            "deleting the row."
        )

        await mon.register_session(
            tmux_name="auto-deregister",
            type="dispatch",
            jsonl_path=jsonl,
            run_dir=sess_dir.parent,
            bead_id="auto-disp",
        )
        await mon.deregister_session("auto-deregister")

        row = _fetch_row(db_path, "auto-deregister")
        assert row is not None, (
            "deregister_session must NOT delete the row — history lookups "
            "depend on it remaining in tmux_sessions."
        )
        assert row["is_live"] == 0, (
            f"Expected is_live=0 after deregister, got {row['is_live']}"
        )
        assert row["jsonl_path"] == str(jsonl), (
            f"jsonl_path must be preserved after deregister; got {row['jsonl_path']!r}"
        )
        assert row["type"] == "dispatch"


# ── Test 7 — historical dispatch session backfill migration ───────────


class TestHistoricalDispatchBackfill:
    """#7 — Migration script back-fills rows for historical agent-runs.

    FAILS TODAY: Before Phase 1 ships, there is no migration that walks
    data/agent-runs/*/sessions/ and inserts a type='dispatch', is_live=0
    row per historical run. This test verifies the migration is present
    and idempotent: before run = no row; after run = row exists with
    jsonl_path and is_live=0.

    The migration is expected to live at:
        tools/dashboard/migrations/backfill_dispatch_sessions.py
    exposing a callable `backfill(agent_runs_dir, db_path=None)`.
    """

    def test_historical_dispatch_session_backfilled(self, monitor_env):
        """Running the backfill creates rows for historical dispatch runs."""
        tmp_path, db_path, _sm_mod = monitor_env

        agent_runs = tmp_path / "agent-runs"
        run_name = "auto-hist-0420-010101"
        sess_dir = agent_runs / run_name / "sessions"
        sess_dir.mkdir(parents=True)
        historical_jsonl = sess_dir / "hist.jsonl"
        historical_jsonl.write_text(
            json.dumps({"type": "system", "content": "ok"}) + "\n"
        )
        # decision.json marks this as a completed dispatch run
        (agent_runs / run_name / "decision.json").write_text(
            json.dumps({"status": "DONE", "bead_id": "auto-hist"})
        )

        # Before backfill: no row exists
        assert _fetch_row(db_path, run_name) is None

        # Import should succeed — attribute-error on miss proves the
        # migration module does not exist on master.
        try:
            from tools.dashboard.migrations import backfill_dispatch_sessions as mig
        except ImportError as exc:
            pytest.fail(
                "tools.dashboard.migrations.backfill_dispatch_sessions missing "
                f"— Phase 1 migration not yet added ({exc})"
            )

        assert hasattr(mig, "backfill"), (
            "backfill_dispatch_sessions.backfill() missing — "
            "expected callable(agent_runs_dir, db_path=None)"
        )

        mig.backfill(agent_runs, db_path=db_path)

        row = _fetch_row(db_path, run_name)
        assert row is not None, (
            f"Backfill did not insert a row for historical run {run_name!r}"
        )
        assert row["type"] == "dispatch", f"type={row['type']!r}, expected 'dispatch'"
        assert row["is_live"] == 0, f"is_live={row['is_live']}, expected 0"
        assert row["jsonl_path"] == str(historical_jsonl), (
            f"jsonl_path not populated: {row['jsonl_path']!r}"
        )

        # Idempotent — running a second time must not raise and must not duplicate
        mig.backfill(agent_runs, db_path=db_path)
        conn = sqlite3.connect(str(db_path))
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM tmux_sessions WHERE tmux_name=?", (run_name,)
        ).fetchone()
        conn.close()
        assert count == 1, f"Backfill is not idempotent — got {count} rows"
