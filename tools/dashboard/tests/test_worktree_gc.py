"""Tombstoned worktree GC — destruction decoupled from state.

Contract (FSM consolidation): ending a session NEVER synchronously
destroys anything. The GC removes a session's worktrees only when ALL of:
- the row is terminal (ENDED/FAILED) with ended_at past the tombstone
  horizon — or the dir has no row at all (orphan),
- the preserve policy (uncommitted changes / local commits) allows it,
- the row is STILL terminal at execution time (a resume can re-enter the
  FSM between scheduling and execution).
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from tools.dashboard.dao import dashboard_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    prior = getattr(dashboard_db, "_conn", None)
    if prior is not None:
        try:
            prior.close()
        except Exception:
            pass
    dashboard_db._conn = None  # type: ignore[attr-defined]
    dashboard_db._DB_PATH = db_path  # type: ignore[attr-defined]
    conn = dashboard_db.get_conn()
    yield conn
    try:
        conn.close()
    except Exception:
        pass
    dashboard_db._conn = None  # type: ignore[attr-defined]


@pytest.fixture
def gc_env(db, tmp_path, monkeypatch):
    """WORKTREES_DIR pointed at tmp with recorded cleanup calls."""
    from tools.dashboard import session_monitor as sm

    wt = tmp_path / "worktrees"
    wt.mkdir()
    monkeypatch.setattr(sm, "WORKTREES_DIR", wt)
    cleaned: list[str] = []
    monkeypatch.setattr(
        sm, "cleanup_session_worktrees",
        lambda name, **kw: cleaned.append(name) or None,
    )
    return {"wt": wt, "cleaned": cleaned, "sm": sm}


def _seed(name: str, state: str, ended_ago: float | None = None) -> None:
    dashboard_db.insert_session(
        tmux_name=name, session_type="container", project="x",
        state="ACTIVE" if state == "ACTIVE" else "LAUNCHING",
    )
    from tools.dashboard.session_lifecycle_worker import STATE_AUTHORITY

    if state in ("ENDED", "FAILED"):
        if state == "ENDED":
            STATE_AUTHORITY.transition(name, "ACTIVE", cause="test")
            STATE_AUTHORITY.transition(name, "ENDED", cause="test")
        else:
            STATE_AUTHORITY.transition(
                name, "FAILED", cause="test", reason="x", failed_phase="s",
            )
        if ended_ago is not None:
            conn = dashboard_db.get_conn()
            conn.execute(
                "UPDATE tmux_sessions SET ended_at=? WHERE tmux_name=?",
                (time.time() - ended_ago, name),
            )
            conn.commit()


def test_gc_removes_only_past_horizon_terminals(gc_env):
    sm = gc_env["sm"]
    horizon = sm.WORKTREE_GC_HORIZON_S
    _seed("auto-old-ended", "ENDED", ended_ago=horizon + 60)
    _seed("auto-fresh-ended", "ENDED", ended_ago=60)
    _seed("auto-active", "ACTIVE")
    _seed("auto-launching", "LAUNCHING")
    for n in ("auto-old-ended", "auto-fresh-ended", "auto-active", "auto-launching"):
        (gc_env["wt"] / n).mkdir()

    sm._worktree_gc_pass()

    assert gc_env["cleaned"] == ["auto-old-ended"]


def test_gc_removes_orphan_dirs_with_no_row(gc_env):
    sm = gc_env["sm"]
    (gc_env["wt"] / "auto-no-row").mkdir()
    sm._worktree_gc_pass()
    assert gc_env["cleaned"] == ["auto-no-row"]


def test_gc_execution_time_recheck_aborts_on_revival(gc_env):
    """The per-dir step re-reads the row: a session revived between the
    keep-set computation and the dir's turn must not be cleaned."""
    sm = gc_env["sm"]
    _seed("auto-t", "ENDED", ended_ago=sm.WORKTREE_GC_HORIZON_S + 60)
    from tools.dashboard.session_lifecycle_worker import STATE_AUTHORITY

    STATE_AUTHORITY.transition(
        "auto-t", "LAUNCHING", phase="requesting", cause="revive",
    )
    sm._gc_worktrees_for_terminal_session("auto-t")
    assert gc_env["cleaned"] == []


def test_death_path_never_cleans_worktrees(db, monkeypatch, tmp_path):
    """A confirmed ACTIVE death transitions the row — it must NOT trigger
    any synchronous worktree removal (destruction is the GC's, later)."""
    from tools.dashboard import session_monitor as sm
    from tools.dashboard import resource_monitor as rm

    dashboard_db.insert_session(
        tmux_name="auto-t", session_type="container", project="x",
        state="ACTIVE",
    )
    m = sm.SessionMonitor()
    m._event_bus = AsyncMock()
    m._event_bus.broadcast = AsyncMock()
    m._final_graph_catchup = AsyncMock()
    monkeypatch.setattr(rm.resource_monitor, "on_session_dead", AsyncMock())
    monkeypatch.setattr(m, "_check_tmux", lambda _n: False)
    cleaned = []
    monkeypatch.setattr(
        sm, "cleanup_session_worktrees",
        lambda name, **kw: cleaned.append(name) or None,
    )

    import asyncio
    rows = dashboard_db.get_live_sessions()
    asyncio.run(m._sweep_tmux_liveness(rows, time.time()))  # miss 1
    asyncio.run(m._sweep_tmux_liveness(rows, time.time()))  # confirmed

    row = dashboard_db.get_session("auto-t")
    assert row["state"] == "ENDED"
    assert cleaned == []  # transition only — no destruction


def test_reader_flip_get_live_sessions_keys_on_state(db):
    """The live set is state-based: terminal rows are excluded,
    LAUNCHING/ACTIVE/STOPPING included — there is no other liveness field
    left to disagree."""
    from tools.dashboard.session_lifecycle_worker import STATE_AUTHORITY

    _ = STATE_AUTHORITY
    dashboard_db.insert_session(
        tmux_name="auto-a", session_type="container", project="x", state="ACTIVE",
    )
    dashboard_db.insert_session(
        tmux_name="auto-l", session_type="container", project="x", state="LAUNCHING",
    )
    dashboard_db.insert_session(
        tmux_name="auto-e", session_type="container", project="x", state="ACTIVE",
    )
    dashboard_db.mark_dead("auto-e")

    names = {r["tmux_name"] for r in dashboard_db.get_live_sessions()}
    assert names == {"auto-a", "auto-l"}
    assert dashboard_db.count_live() == 2
