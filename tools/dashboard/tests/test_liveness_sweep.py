"""Liveness-sweep reap semantics (_sweep_tmux_liveness).

The sweep's contract: a session is reaped only on CONSECUTIVE
authoritative "no such session" probe results. A probe that fails to run
at all (fork EAGAIN under process pressure, missing tmux binary — any
OSError) is UNKNOWN and must never count toward death.

Reproduction of the 2026-07-08 18:1x incident: a user-level fork-EAGAIN
spike made every `tmux has-session` spawn raise BlockingIOError in the
same sweep tick; the then-boolean probe returned False for every row and
the sweep reaped the entire live fleet at once — every claude process
was actually alive.
"""
from __future__ import annotations

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
def monitor(monkeypatch):
    from tools.dashboard import session_monitor as sm

    m = sm.SessionMonitor()
    m._event_bus = AsyncMock()
    m._event_bus.broadcast = AsyncMock()
    m._final_graph_catchup = AsyncMock()
    # The death path fires a detached resource snapshot — stub it out.
    from tools.dashboard import resource_monitor as rm
    monkeypatch.setattr(rm.resource_monitor, "on_session_dead", AsyncMock())
    yield m


def _seed_live(*names: str) -> list[dict]:
    for n in names:
        dashboard_db.insert_session(
            tmux_name=n, session_type="container", project="x",
        )
    return dashboard_db.get_live_sessions()


def _live_names() -> set[str]:
    return {r["tmux_name"] for r in dashboard_db.get_live_sessions()}


@pytest.mark.asyncio
async def test_fork_eagain_spike_reaps_nothing(db, monitor, monkeypatch):
    """THE INCIDENT: every probe raises BlockingIOError in one tick.

    No session may die, no worktree cleanup may run — repeatedly."""
    from tools.dashboard import session_monitor as sm

    rows = _seed_live("auto-a", "auto-b", "auto-c")

    def eagain_spawn(*_a, **_kw):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(sm.subprocess, "run", eagain_spawn)
    cleanups = []
    monkeypatch.setattr(
        sm, "_cleanup_worktrees_for_dead_session",
        lambda name: cleanups.append(name),
    )

    import time
    for _tick in range(5):  # a sustained spike, several sweep ticks long
        changed = await monitor._sweep_tmux_liveness(rows, time.time())
        assert changed is False

    assert _live_names() == {"auto-a", "auto-b", "auto-c"}
    assert cleanups == []


@pytest.mark.asyncio
async def test_confirmed_death_requires_consecutive_misses(db, monitor, monkeypatch):
    from tools.dashboard import session_monitor as sm

    rows = _seed_live("auto-a")
    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: False)
    cleanups = []
    monkeypatch.setattr(
        sm, "_cleanup_worktrees_for_dead_session",
        lambda name: cleanups.append(name),
    )

    import time
    changed = await monitor._sweep_tmux_liveness(rows, time.time())
    assert changed is False  # first authoritative miss — not yet
    assert _live_names() == {"auto-a"}

    changed = await monitor._sweep_tmux_liveness(rows, time.time())
    assert changed is True  # second consecutive miss — confirmed dead
    assert _live_names() == set()
    assert cleanups == ["auto-a"]


@pytest.mark.asyncio
async def test_alive_probe_resets_miss_count(db, monitor, monkeypatch):
    rows = _seed_live("auto-a")
    import time

    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: False)
    await monitor._sweep_tmux_liveness(rows, time.time())  # miss 1

    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: True)
    await monitor._sweep_tmux_liveness(rows, time.time())  # alive — reset

    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: False)
    changed = await monitor._sweep_tmux_liveness(rows, time.time())  # miss 1 again
    assert changed is False
    assert _live_names() == {"auto-a"}


@pytest.mark.asyncio
async def test_probe_failure_does_not_advance_or_reset_a_miss_streak(db, monitor, monkeypatch):
    """miss → UNKNOWN → miss must count as 2 consecutive misses: the
    unknown tick contributes nothing in either direction."""
    from tools.dashboard import session_monitor as sm

    rows = _seed_live("auto-a")
    cleanups = []
    monkeypatch.setattr(
        sm, "_cleanup_worktrees_for_dead_session",
        lambda name: cleanups.append(name),
    )
    import time

    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: False)
    await monitor._sweep_tmux_liveness(rows, time.time())  # miss 1
    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: None)
    await monitor._sweep_tmux_liveness(rows, time.time())  # unknown
    assert _live_names() == {"auto-a"}
    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: False)
    changed = await monitor._sweep_tmux_liveness(rows, time.time())  # miss 2
    assert changed is True
    assert cleanups == ["auto-a"]


def test_check_tmux_probe_failure_is_unknown(monkeypatch):
    from tools.dashboard import session_monitor as sm

    def eagain_spawn(*_a, **_kw):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(sm.subprocess, "run", eagain_spawn)
    assert sm.SessionMonitor._check_tmux("auto-x") is None


# ── Phase B: the reaper reads the one state ──────────────────────────


def _seed_state(name: str, state: str, phase: str | None = None) -> None:
    from tools.dashboard.session_lifecycle_worker import STATE_AUTHORITY

    dashboard_db.insert_session(
        tmux_name=name, session_type="container", project="x",
        state="LAUNCHING" if state != "ACTIVE" else "ACTIVE",
    )
    if state == "LAUNCHING" and phase:
        STATE_AUTHORITY.transition(name, "LAUNCHING", phase=phase, cause="test")
    elif state == "STOPPING":
        STATE_AUTHORITY.transition(name, "STOPPING", phase="stopping", cause="test")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [
    "requesting", "preparing_workspace", "launching_container",
    "setup_running", "harness_starting", "confirming_trust",
    "composer_ready", "awaiting_first_response",
])
async def test_reaper_never_reaps_launching_within_budget(db, monitor, monkeypatch, phase):
    """THE class test: for every launch phase, a missing tmux does not
    reap a LAUNCHING session inside its own budget — allowlist-omission
    bugs (setup_running, confirming_trust, …) are impossible by
    construction because there is no allowlist, only the budget table."""
    from tools.dashboard import session_monitor as sm

    _seed_state("auto-a", "LAUNCHING", phase=phase)
    rows = dashboard_db.get_live_sessions()
    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: False)
    cleanups = []
    monkeypatch.setattr(
        sm, "_cleanup_worktrees_for_dead_session",
        lambda name: cleanups.append(name),
    )
    import time
    changed = await monitor._sweep_tmux_liveness(rows, time.time())
    assert changed is False
    row = dashboard_db.get_session("auto-a")
    assert row["state"] == "LAUNCHING"
    assert cleanups == []


@pytest.mark.asyncio
async def test_reaper_never_touches_stopping(db, monitor, monkeypatch):
    _seed_state("auto-a", "STOPPING")
    rows = dashboard_db.get_live_sessions()
    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: False)
    import time
    changed = await monitor._sweep_tmux_liveness(rows, time.time())
    assert changed is False
    assert dashboard_db.get_session("auto-a")["state"] == "STOPPING"


@pytest.mark.asyncio
async def test_orphaned_launch_fails_after_phase_budget_plus_belt(db, monitor, monkeypatch):
    """A LAUNCHING row whose last transition is older than its phase's own
    budget + the belt margin is an orphan (the worker lost the job) — it
    becomes FAILED (retryable), never ENDED: nothing was ever running."""
    from tools.dashboard.session_lifecycle_worker import (
        REAPER_BELT_MARGIN_S,
        STEP_TIMEOUTS_S,
    )

    _seed_state("auto-a", "LAUNCHING", phase="setup_running")
    rows = dashboard_db.get_live_sessions()
    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: False)
    import time
    late = time.time() + STEP_TIMEOUTS_S["setup_running"] + REAPER_BELT_MARGIN_S + 5
    changed = await monitor._sweep_tmux_liveness(rows, late)
    assert changed is True
    row = dashboard_db.get_session("auto-a")
    assert row["state"] == "FAILED"
    assert "orphaned" in row["lifecycle_detail"]


@pytest.mark.asyncio
async def test_orphan_belt_respects_the_slowest_budget(db, monitor, monkeypatch):
    """setup_running is safe for its full 600s budget — the 300s-grace <
    600s-setup ordering bug is unrepresentable because the belt reads the
    worker's own table."""
    from tools.dashboard.session_lifecycle_worker import STEP_TIMEOUTS_S

    _seed_state("auto-a", "LAUNCHING", phase="setup_running")
    rows = dashboard_db.get_live_sessions()
    monkeypatch.setattr(monitor, "_check_tmux", lambda _n: False)
    import time
    mid_setup = time.time() + STEP_TIMEOUTS_S["setup_running"] - 30
    changed = await monitor._sweep_tmux_liveness(rows, mid_setup)
    assert changed is False
    assert dashboard_db.get_session("auto-a")["state"] == "LAUNCHING"


def test_step_budgets_are_single_sourced():
    from tools.dashboard import server
    from tools.dashboard.session_lifecycle_worker import STEP_TIMEOUTS_S

    assert server._LIFECYCLE_SETUP_TIMEOUT_S == STEP_TIMEOUTS_S["setup_running"]
    assert server._LIFECYCLE_PREPARING_TIMEOUT_S == STEP_TIMEOUTS_S["preparing_workspace"]
    assert server._LIFECYCLE_LAUNCHING_TIMEOUT_S == STEP_TIMEOUTS_S["launching_container"]
    assert server._LIFECYCLE_WAITING_READY_TIMEOUT_S == STEP_TIMEOUTS_S["harness_starting"]
