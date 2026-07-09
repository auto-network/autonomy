"""Tests for the worker-owned startup_state FSM's monitor-side surface.

The lifecycle worker's SessionLifecycleStateWriter owns every transition;
the monitor contributes exactly two things, covered here:

- ``arm_startup_state``: the explicit FSM entry (create/resume/retry seed
  the row and arm the pane-poller before enqueueing).
- ``update_phase``: transient sub-phase progress only (repo N/M during
  prepare) — it can no longer write lifecycle state at all.

Writer-side transition semantics live in test_session_lifecycle_worker.py;
the no-other-writers guarantee is pinned by test_no_racing_writers.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tools.dashboard.dao import dashboard_db
from tools.dashboard.session_lifecycle_worker import SessionLifecycleStateWriter


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Pin dashboard.db to a fresh per-test sqlite file.

    Closes the prior module-level connection before reinit so the SQLite
    write lock from the previous test releases — otherwise later writes
    fail with "database is locked".
    """
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
def monitor():
    """SessionMonitor with a mock event_bus so broadcasts are observable."""
    from tools.dashboard.session_monitor import SessionMonitor
    m = SessionMonitor()
    m._event_bus = AsyncMock()
    m._event_bus.broadcast = AsyncMock()
    yield m


def _read_state(tmux_name: str) -> str | None:
    conn = dashboard_db.get_conn()
    row = conn.execute(
        "SELECT startup_state FROM tmux_sessions WHERE tmux_name = ?", (tmux_name,),
    ).fetchone()
    return None if row is None else row[0]


def _broadcast_count(monitor) -> int:
    return monitor._event_bus.broadcast.await_count


# ── register_pending: seeds + arms the FSM ──────────────────────────


@pytest.mark.asyncio
async def test_register_pending_seeds_requesting(db, monitor):
    await monitor.register_pending(
        "auto-test", session_type="container", project="enterprise-ng", harness="claude",
    )
    assert _read_state("auto-test") == "requesting"


@pytest.mark.asyncio
async def test_register_pending_idempotent_on_duplicate(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.register_pending("auto-test", project="x")
    assert _read_state("auto-test") == "requesting"


@pytest.mark.asyncio
async def test_register_after_pending_preserves_startup_state(db, monitor):
    await monitor.register_pending("host-test", session_type="host", project="host-proj")
    SessionLifecycleStateWriter().set_state("host-test", "waiting_ready")

    await monitor.register("host-test", session_type="host", project="host-proj")

    assert _read_state("host-test") == "harness_starting"


# ── arm_startup_state: the explicit FSM entry ───────────────────────


@pytest.mark.asyncio
async def test_arm_re_enters_after_ended(db, monitor):
    """arm re-enters the FSM on terminal rows — the resume/retry entry
    path (ENDED→LAUNCHING / FAILED→LAUNCHING in the legality matrix)."""
    await monitor.register_pending("auto-test", project="x")
    SessionLifecycleStateWriter().set_state("auto-test", "running")
    SessionLifecycleStateWriter().set_state("auto-test", "dead")
    assert _read_state("auto-test") is None
    changed = await monitor.arm_startup_state("auto-test", "harness_starting")
    assert changed is True
    assert _read_state("auto-test") == "harness_starting"
    row = dashboard_db.get_session("auto-test")
    assert row["state"] == "LAUNCHING"
    assert row["ended_at"] is None  # cleared on FSM re-entry


@pytest.mark.asyncio
async def test_arm_overwrites_setup_failed(db, monitor):
    """An explicit relaunch is the retry path — sticky failure must not
    block it."""
    await monitor.register_pending("auto-test", project="x")
    SessionLifecycleStateWriter().fail("auto-test", phase="setup", reason="boom")
    changed = await monitor.arm_startup_state("auto-test", "harness_starting")
    assert changed is True
    assert _read_state("auto-test") == "harness_starting"


@pytest.mark.asyncio
async def test_arm_arms_screen_poll_watch(db, monitor):
    """arm must put the session in the pane-poller's armed set — the
    poller's ONLY watch predicate — so composer detection runs for this
    launch and stops at composer_ready/death."""
    dashboard_db.insert_session(
        tmux_name="auto-test", session_type="container", project="x",
    )
    dashboard_db.mark_dead("auto-test")
    await monitor.arm_startup_state("auto-test", "harness_starting")
    assert "auto-test" in monitor._screen_poll_armed


@pytest.mark.asyncio
async def test_arm_broadcasts_on_change(db, monitor):
    dashboard_db.insert_session(
        tmux_name="auto-test", session_type="container", project="x",
    )
    dashboard_db.mark_dead("auto-test")
    monitor._event_bus.broadcast.reset_mock()
    await monitor.arm_startup_state("auto-test", "harness_starting")
    assert _broadcast_count(monitor) == 1


@pytest.mark.asyncio
async def test_arm_refused_on_active_row(db, monitor):
    """The legality matrix forbids re-launching a session that is ACTIVE —
    a resume/retry can only enter from a terminal state (the API's
    live-session guard makes this unreachable in practice; the matrix
    makes it impossible)."""
    dashboard_db.insert_session(
        tmux_name="auto-test", session_type="container", project="x",
    )  # born ACTIVE
    changed = await monitor.arm_startup_state("auto-test", "harness_starting")
    assert changed is False
    row = dashboard_db.get_session("auto-test")
    assert row["state"] == "ACTIVE"
    assert row["startup_state"] is None


@pytest.mark.asyncio
async def test_arm_unknown_session_no_write(db, monitor):
    changed = await monitor.arm_startup_state("auto-nonexistent", "harness_starting")
    assert changed is False


# ── update_phase: progress-only ─────────────────────────────────────


@pytest.mark.asyncio
async def test_update_phase_progress_only_broadcasts(db, monitor):
    """progress updates broadcast once so the in-memory progress dict
    flushes to the registry payload — and never touch startup_state."""
    await monitor.register_pending("auto-test", project="x")
    monitor._event_bus.broadcast.reset_mock()
    await monitor.update_phase(
        "auto-test",
        progress={"repo_index": 2, "total": 3, "current_repo": "autonomy"},
    )
    assert _broadcast_count(monitor) == 1
    assert monitor._phase_progress["auto-test"]["repo_index"] == 2
    assert _read_state("auto-test") == "requesting"  # untouched


@pytest.mark.asyncio
async def test_update_phase_empty_dict_clears_progress(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.update_phase("auto-test", progress={"repo_index": 1, "total": 2})
    await monitor.update_phase("auto-test", progress={})
    assert "auto-test" not in monitor._phase_progress


@pytest.mark.asyncio
async def test_update_phase_none_is_a_no_op(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.update_phase("auto-test", progress={"repo_index": 1, "total": 2})
    monitor._event_bus.broadcast.reset_mock()
    await monitor.update_phase("auto-test", progress=None)
    assert monitor._phase_progress["auto-test"]["repo_index"] == 1
    assert _broadcast_count(monitor) == 0


# ── Registry payload includes startup_state ─────────────────────────


@pytest.mark.asyncio
async def test_registry_payload_includes_startup_state(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    SessionLifecycleStateWriter().set_state("auto-test", "preparing")
    payload = monitor.get_registry()
    entry = next(e for e in payload if e["session_id"] == "auto-test")
    assert entry["startup_state"] == "preparing_workspace"


@pytest.mark.asyncio
async def test_registry_payload_startup_state_null_after_running(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    SessionLifecycleStateWriter().set_state("auto-test", "running")
    payload = monitor.get_registry()
    entry = next(e for e in payload if e["session_id"] == "auto-test")
    assert entry["startup_state"] is None


# ── Liveness sweep vs in-flight launches (the resume-reap race) ─────


@pytest.mark.asyncio
async def test_arm_stamps_last_activity(db, monitor):
    """The liveness sweep's booting-grace anchors on last_activity; arm must
    stamp it or a resumed row (ancient created_at, stale last_activity)
    gets reaped while queued at 'requesting'."""
    import time as _time

    dashboard_db.insert_session(
        tmux_name="auto-test", session_type="container", project="x",
    )
    dashboard_db.mark_dead("auto-test")  # arm enters from a terminal state
    conn = dashboard_db.get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET last_activity=? WHERE tmux_name=?",
        (_time.time() - 99999, "auto-test"),
    )
    conn.commit()
    await monitor.arm_startup_state("auto-test", "requesting")
    row = dashboard_db.get_session("auto-test")
    assert row["last_activity"] > _time.time() - 5

