"""Reproducers for session_monitor JSONL discovery races (auto-4yznw).

Two bugs:

  1. Race in ``_add_dir_watch`` — when a container's session directory and
     its JSONL are created in the same kernel batch, the IN_CREATE event
     for the JSONL is delivered *before* the inotify watch on the subdir
     lands, so the monitor never discovers the file and the session stays
     ``jsonl=pending`` forever.

  2. ``/api/session/tail`` fallback returns the file but doesn't persist
     the resolved ``jsonl_path`` back into ``dashboard.db``; subsequent
     SSE tails therefore never activate.

TDD discipline: these tests must FAIL on master (red) and PASS after the
fix (green). They cover four scenarios:

  1.1  race: JSONL materialises inside a subdir before the subdir watch
       is effective. Scan-on-watch-add is the fix.
  1.2  dashboard restart: JSONL is on disk at registration time. Same
       scan-on-watch-add path covers it.
  1.3  tail fallback: ``api_session_tail`` falls back to
       ``resolve_session_file``. The resolved path must be persisted.
  1.4  periodic reconciliation: both the scan and the fallback missed
       the file; a background tick eventually discovers it.
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

pytest.importorskip("inotify_simple")
pytest.importorskip("pytest_asyncio")


# ── Helpers ────────────────────────────────────────────────────────────


def _init_test_db(db_path: Path) -> None:
    """Minimal dashboard.db schema sufficient for session_monitor registration."""
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
        curr_jsonl_file TEXT,
        activity_state TEXT DEFAULT 'idle',
        todos TEXT DEFAULT '[]'
    )""")
    conn.commit()
    conn.close()


def _db_row(db_path: Path, tmux_name: str) -> dict | None:
    """Read a row directly (bypasses the module's cached conn to avoid stale reads)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    """Test DB + isolated agent-runs root + stubbed `graph ingest-session`."""
    db_path = tmp_path / "dashboard.db"
    _init_test_db(db_path)
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))

    agent_runs = tmp_path / "agent-runs"
    agent_runs.mkdir()
    monkeypatch.setenv("DASHBOARD_AGENT_RUNS_DIR", str(agent_runs))

    # `link_and_enrich` calls `graph ingest-session`; we don't want the test
    # to depend on the real graph CLI being installed/initialised. Replace
    # with a stub that returns a fake source id.
    import subprocess as _sp

    real_run = _sp.run

    class _CompletedStub:
        def __init__(self, stdout: str = "stub-graph-id\n"):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "graph":
            return _CompletedStub()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_sp, "run", fake_run)

    # Reload DAO + session_monitor module so they bind to the new env.
    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from tools.dashboard import session_monitor as sm_mod
    importlib.reload(sm_mod)

    yield tmp_path, db_path, agent_runs


def _make_monitor(setup_env):
    """Fresh SessionMonitor bound to the reloaded module."""
    from tools.dashboard.session_monitor import SessionMonitor
    mon = SessionMonitor()
    mon._init_inotify()
    assert mon._use_inotify, "tests require real inotify_simple"
    return mon


# ── 1.1  Race: JSONL lands before subdir watch is effective ────────────


@pytest.mark.asyncio
async def test_jsonl_created_before_subwatch_added(setup_env):
    """Reproduces the race where the JSONL exists inside a subdir by the
    time ``_add_dir_watch(subdir)`` is called.

    The fix (scan-on-watch-add) must discover the JSONL that was already
    on disk when the watch was established and promote the session.
    """
    tmp_path, db_path, _ = setup_env
    run_dir = tmp_path / "agent-runs" / "auto-test-001-20260421-000000"
    sessions_dir = run_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    mon = _make_monitor(setup_env)

    # Register a container session with resolution_dir pointing at sessions/
    await mon.register(
        tmux_name="auto-test-001",
        session_type="container",
        project="enterprise-ng",
        resolution_dir=sessions_dir,
    )

    # Parent watch is set on sessions_dir. Before any IN_CREATE for the
    # subdir can be processed, both the subdir AND its JSONL materialise
    # — reproducing the same-kernel-batch race. We emulate the race by
    # doing both writes up-front and then invoking `_add_dir_watch` on
    # the subdir (the exact call site used by `_handle_in_create` when
    # it sees a new subdir).
    subdir = sessions_dir / "-workspace-enterprise-ng"
    subdir.mkdir()
    jsonl = subdir / "uuid-abc.jsonl"
    jsonl.write_text('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}}\n')

    # Simulate the IN_CREATE handler's subdir branch:
    mon._add_dir_watch("auto-test-001", str(subdir))

    # Give async scheduling a tick (scan-on-add may schedule a coroutine).
    await asyncio.sleep(0.05)

    state = mon._tail_states.get("auto-test-001")
    assert state is not None, "TailState should exist after register"
    assert state.needs_resolution is False, (
        "scan-on-watch-add must promote the session out of pending state"
    )

    row = _db_row(db_path, "auto-test-001")
    assert row is not None
    assert row["jsonl_path"] == str(jsonl), (
        f"jsonl_path should be persisted; got {row['jsonl_path']!r}"
    )


# ── 1.2  Pre-existing JSONL at registration time ───────────────────────


@pytest.mark.asyncio
async def test_jsonl_exists_when_watch_added(setup_env):
    """Dashboard-restart variant: subdir + JSONL exist on disk already.

    No IN_CREATE events will ever fire; only the scan-on-add pass inside
    ``_add_dir_watch`` / ``register`` can discover the file.
    """
    tmp_path, db_path, _ = setup_env
    run_dir = tmp_path / "agent-runs" / "auto-test-002-20260421-000000"
    sessions_dir = run_dir / "sessions"
    subdir = sessions_dir / "-workspace-enterprise-ng"
    subdir.mkdir(parents=True)
    jsonl = subdir / "uuid-def.jsonl"
    jsonl.write_text('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hello"}]}}\n')

    mon = _make_monitor(setup_env)

    await mon.register(
        tmux_name="auto-test-002",
        session_type="container",
        project="enterprise-ng",
        resolution_dir=sessions_dir,
    )
    # Registration must, on its own, walk the resolution_dir + existing
    # subdirs and discover the JSONL. No tick required.
    await asyncio.sleep(0.05)

    state = mon._tail_states.get("auto-test-002")
    assert state is not None
    assert state.needs_resolution is False

    row = _db_row(db_path, "auto-test-002")
    assert row is not None
    assert row["jsonl_path"] == str(jsonl)


# ── 1.3  Tail fallback must persist the resolved path ──────────────────


def test_tail_fallback_persists_jsonl_path(setup_env):
    """``/api/session/tail`` fallback must write back the resolved path.

    Otherwise every request pays the full-tree scan cost and the SSE tail
    never activates because ``_tail_one`` skips rows with no jsonl_path.
    """
    tmp_path, db_path, agent_runs = setup_env

    # Seed a container session with NO jsonl_path.
    tmux_name = "auto-test-003"
    run_dir = agent_runs / f"{tmux_name}-20260421-000000"
    sessions_dir = run_dir / "sessions" / "-workspace-enterprise-ng"
    sessions_dir.mkdir(parents=True)
    jsonl = sessions_dir / "uuid-ghi.jsonl"
    jsonl.write_text('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"row"}]}}\n')

    from tools.dashboard.dao.dashboard_db import insert_session
    insert_session(
        tmux_name=tmux_name,
        session_type="container",
        project="enterprise-ng",
        jsonl_path=None,
        resolution_dir=str(run_dir / "sessions"),
    )

    # Patch the server's session_monitor + tmux check so the app boots.
    import importlib
    from tools.dashboard import server as srv_mod
    importlib.reload(srv_mod)

    with patch.object(
        srv_mod.session_monitor.__class__, "_check_tmux",
        staticmethod(lambda name: True),
    ):
        from starlette.testclient import TestClient
        with TestClient(srv_mod.app) as client:
            resp = client.get(f"/api/session/enterprise-ng/{tmux_name}/tail")
            assert resp.status_code == 200
            body = resp.json()
            # The endpoint must return the discovered entry.
            assert body.get("entries"), (
                f"fallback should have served entries; body={body!r}"
            )

    # KEY ASSERTION: the resolved path must be persisted.
    row = _db_row(db_path, tmux_name)
    assert row is not None
    assert row["jsonl_path"] == str(jsonl), (
        f"fallback must persist jsonl_path; got {row['jsonl_path']!r}"
    )


# ── 1.4  Periodic reconciliation catches stragglers ────────────────────


@pytest.mark.asyncio
async def test_periodic_reconciliation_resolves_stragglers(setup_env):
    """Backstop for the case where scan-on-add *and* the first tail miss.

    The reconciliation task must periodically re-scan ``needs_resolution``
    sessions and promote them when their JSONL appears.
    """
    tmp_path, db_path, agent_runs = setup_env

    tmux_name = "auto-test-004"
    run_dir = agent_runs / f"{tmux_name}-20260421-000000"
    sessions_dir = run_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    mon = _make_monitor(setup_env)

    await mon.register(
        tmux_name=tmux_name,
        session_type="container",
        project="enterprise-ng",
        resolution_dir=sessions_dir,
    )
    # At registration, nothing exists yet — session is pending.
    assert mon._tail_states[tmux_name].needs_resolution is True

    # Sometime later, the JSONL appears — but we never fire an inotify
    # event for it (simulating a total IN_CREATE miss).
    subdir = sessions_dir / "-workspace-enterprise-ng"
    subdir.mkdir()
    jsonl = subdir / "uuid-jkl.jsonl"
    jsonl.write_text('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"late"}]}}\n')

    # Drive the reconciliation directly. Implementations may expose this
    # as an async method (`reconciliation_tick`) or a coroutine-bearing
    # loop; require the discoverable method.
    assert hasattr(mon, "reconciliation_tick"), (
        "SessionMonitor must expose reconciliation_tick() for the backstop"
    )
    await mon.reconciliation_tick()

    state = mon._tail_states[tmux_name]
    assert state.needs_resolution is False, (
        "reconciliation must have promoted the session once the JSONL exists"
    )

    row = _db_row(db_path, tmux_name)
    assert row is not None
    assert row["jsonl_path"] == str(jsonl)
