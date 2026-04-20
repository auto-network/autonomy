"""
Phase 0 — Session monitor consolidation: L1 unit misc tests.

Bead auto-ylj6r. Additional L1 unit tests that don't need a running
monitor — they read source files / inspect Python modules directly.
All MUST FAIL on master.

Covered tests from the bead's Phase 0 test table:
  #4 test_session_store_default_isLive_false
  #5 test_dispatcher_has_no_jsonl_reader_function
  #6 test_recent_card_session_id_always_tmux_session
  #8 test_dispatch_tail_scans_agent_runs_fallback
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest


_DASHBOARD = Path(__file__).resolve().parents[2]
_JS_DIR = _DASHBOARD / "static" / "js"


# ── Test 4 — session-store default isLive is false ─────────────────────


class TestSessionStoreDefaultIsLiveFalse:
    """#4 — getSessionStore(new_id).isLive === false (default off).

    FAILS TODAY: session-store.js:60 sets `isLive: true` as the default.
    Phase 4 flips this default to false so that clicking an unknown/Recent
    session_id does not spawn a phantom live entry in the Active list.
    """

    def test_session_store_default_isLive_false(self):
        store_js = (_JS_DIR / "lib" / "session-store.js").read_text()

        assert "isLive: false" in store_js, (
            "session-store.js must declare `isLive: false` as the store default. "
            "Master currently declares `isLive: true` at around line 60, which "
            "causes Recent-card clicks to create phantom Active entries for "
            "sessions that have no backing JSONL yet."
        )
        # Guard: make sure the default line is actually the seed default,
        # not a conditional branch somewhere else in the file. We assert
        # the legacy `isLive: true` seed literal is gone.
        # Allow references like `store.isLive = true` (conditional) but not
        # the object-literal seed form.
        assert "  isLive: true," not in store_js and \
               "    isLive: true," not in store_js, (
            "session-store.js still contains `isLive: true,` seed — "
            "Phase 4 flip not done."
        )


# ── Test 5 — dispatcher no longer owns _read_jsonl_incremental ─────────


class TestDispatcherHasNoJsonlReader:
    """#5 — hasattr(dispatcher, '_read_jsonl_incremental') must be False.

    FAILS TODAY: agents/dispatcher.py:1365 defines
    `_read_jsonl_incremental` — this is one of the three independent
    JSONL readers the bead deletes. Phase 2 replaces dispatcher's own
    reader with a call to `session_monitor.get_session_stats(session_id)`.
    """

    def test_dispatcher_has_no_jsonl_reader_function(self):
        from agents import dispatcher
        assert not hasattr(dispatcher, "_read_jsonl_incremental"), (
            "agents.dispatcher._read_jsonl_incremental still exists — "
            "Phase 2 has not yet deleted the duplicate reader. The bead "
            "requires dispatcher card stats to read through session_monitor "
            "(get_session_stats) so all three consumers see the same state."
        )


# ── Test 6 — Recent card sessionId is tmux_session only ────────────────


class TestRecentCardSessionIdMapping:
    """#6 — sessionId for Recent cards must be `r.tmux_session` (no fallback).

    FAILS TODAY: sessions.js:502 reads
        var sessionId = r.tmux_session || r.session_uuid || r.id;
    The fallback chain means dispatch/librarian rows (with no tmux_session
    after the session ended) fall through to session_uuid / r.id, which
    cannot be resolved by the tail endpoint and spawn phantom Active cards.
    Phase 4 removes the fallback and demands tmux_session be present.
    """

    def test_recent_card_session_id_always_tmux_session(self):
        sessions_js = (_JS_DIR / "pages" / "sessions.js").read_text()

        # The old fallback chain must be gone.
        assert "r.tmux_session || r.session_uuid || r.id" not in sessions_js, (
            "sessions.js still contains the 3-way fallback "
            "`r.tmux_session || r.session_uuid || r.id` at ~line 502. "
            "Phase 4 replaces this with `r.tmux_session` only."
        )
        # And the second-safety-net on the map() result must also be gone.
        assert "tmux_session: r.tmux_session || sessionId" not in sessions_js, (
            "sessions.js:_fetchRecent still contains the "
            "`tmux_session: r.tmux_session || sessionId` safety-net, which "
            "papers over unresolved rows. Phase 4 removes this."
        )


# ── Test 8 — Last-ditch resolver scans agent-runs/ ─────────────────────


class TestDispatchTailAgentRunsFallback:
    """#8 — Fallback resolver scans data/agent-runs/ when DB has no match.

    FAILS TODAY: No such fallback exists on master. If a dispatch session
    was pruned from the monitor or never registered, the tail endpoint
    has no way to locate the JSONL by scanning agent-runs/. Phase 3 adds
    a helper (e.g. `session_monitor.resolve_session_file(session_id)` or
    a server helper `_resolve_via_agent_runs`) that scans
    data/agent-runs/*/sessions/<project>/<id>.jsonl as a last resort.

    Marked optional in the bead ("can be deferred"), but we encode the
    contract so Phase 3 has a clear signal if the fallback lands.
    """

    def test_dispatch_tail_scans_agent_runs_fallback(self, tmp_path):
        import os
        # Set up a dashboard.db with NO matching session
        db_path = tmp_path / "dashboard.db"
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

        # Plant a JSONL in agent-runs/ that only the fallback could find.
        agent_runs = tmp_path / "agent-runs"
        run_dir = agent_runs / "auto-fallback-0420-030303" / "sessions" / "autonomy"
        run_dir.mkdir(parents=True)
        fallback_uuid = "ffffffff-aaaa-bbbb-cccc-000000000001"
        jsonl = run_dir / f"{fallback_uuid}.jsonl"
        jsonl.write_text('{"type":"system","content":"hello"}\n')

        os.environ["DASHBOARD_DB"] = str(db_path)
        os.environ["DASHBOARD_AGENT_RUNS_DIR"] = str(agent_runs)
        try:
            import importlib
            from tools.dashboard.dao import dashboard_db as db_mod
            importlib.reload(db_mod)
            from tools.dashboard import session_monitor as sm_mod
            importlib.reload(sm_mod)

            # Expected API — a classmethod or module function that scans
            # agent-runs for a given session_id and returns the file path.
            # Any of these are acceptable; test each.
            candidate_impls = []
            mon = sm_mod.SessionMonitor()
            for attr in ("resolve_session_file", "find_session_by_uuid",
                         "resolve_agent_run"):
                if hasattr(mon, attr):
                    candidate_impls.append(getattr(mon, attr))

            from tools.dashboard import server as server_mod
            importlib.reload(server_mod)
            for attr in ("_resolve_via_agent_runs", "_resolve_dispatch_fallback"):
                if hasattr(server_mod, attr):
                    candidate_impls.append(getattr(server_mod, attr))

            assert candidate_impls, (
                "No agent-runs/ fallback resolver found. Expected one of:\n"
                "  session_monitor.resolve_session_file(session_id)\n"
                "  session_monitor.find_session_by_uuid(session_id)\n"
                "  server._resolve_via_agent_runs(session_id)\n"
                "  server._resolve_dispatch_fallback(session_id)\n"
                "Phase 3 should add one of these to cover pruned/historical "
                "dispatch sessions that the tmux_sessions DB cannot resolve."
            )
        finally:
            os.environ.pop("DASHBOARD_DB", None)
            os.environ.pop("DASHBOARD_AGENT_RUNS_DIR", None)
