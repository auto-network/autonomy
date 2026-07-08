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
