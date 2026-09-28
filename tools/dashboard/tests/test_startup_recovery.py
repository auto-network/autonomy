"""Startup recovery sweep (_recover_stuck_lifecycle_rows).

FSM contract, correctness addition 3: the lifecycle queue is process
memory, so a dashboard restart mid-launch strands rows in a non-terminal
startup_state with is_live=1. The boot sweep must adopt rows whose tmux
session survived (clear startup_state) and fail rows whose process is
gone (failed(startup_recovery), retryable), while leaving healthy and
sticky-failed rows untouched. A launch still within its step budget is
still launching (a hot reload overlaps the old worker with the new one,
auto-2btus): it is re-checked after the budget and resolved only if it
has not moved.
"""
from __future__ import annotations

import json
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


def _seed(tmux_name: str, startup_state: str | None) -> None:
    from tools.dashboard.session_lifecycle_worker import STATE_AUTHORITY

    if startup_state is None:
        dashboard_db.insert_session(
            tmux_name=tmux_name, session_type="container", project="x",
            state="ACTIVE",
        )
        return
    dashboard_db.insert_session(
        tmux_name=tmux_name, session_type="container", project="x",
        state="LAUNCHING",
    )
    if startup_state == "setup_failed":
        STATE_AUTHORITY.transition(
            tmux_name, "FAILED", cause="test", reason="x", failed_phase="s",
        )
    else:
        STATE_AUTHORITY.transition(
            tmux_name, "LAUNCHING", phase=startup_state, cause="test",
        )


def _backdate(tmux_name: str, seconds: float = 3600.0) -> None:
    """Put the row's last transition *seconds* in the past (past any step budget)."""
    import time

    conn = dashboard_db.get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET last_activity=? WHERE tmux_name=?",
        (time.time() - seconds, tmux_name),
    )
    conn.commit()


def _row(tmux_name: str) -> dict:
    conn = dashboard_db.get_conn()
    cur = conn.execute(
        "SELECT startup_state, state, lifecycle_detail, ended_at"
        " FROM tmux_sessions WHERE tmux_name=?",
        (tmux_name,),
    )
    row = cur.fetchone()
    keys = ("startup_state", "state", "lifecycle_detail", "ended_at")
    return dict(zip(keys, row))


async def _run_sweep(monkeypatch, alive: set[str]):
    from tools.dashboard import server

    monkeypatch.setattr(server, "_tmux_session_exists", lambda name: name in alive)
    server.session_monitor._event_bus = AsyncMock()
    server.session_monitor._event_bus.broadcast = AsyncMock()
    await server._recover_stuck_lifecycle_rows()


@pytest.mark.asyncio
async def test_adopts_row_when_tmux_alive(db, monkeypatch):
    _seed("auto-stuck", "setup_running")
    _backdate("auto-stuck")
    await _run_sweep(monkeypatch, alive={"auto-stuck"})
    row = _row("auto-stuck")
    assert row["startup_state"] is None
    assert row["state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_fails_row_when_tmux_gone(db, monkeypatch):
    _seed("auto-zombie", "harness_starting")
    _backdate("auto-zombie")
    await _run_sweep(monkeypatch, alive=set())
    row = _row("auto-zombie")
    assert row["startup_state"] == "setup_failed"
    assert row["state"] == "FAILED"
    detail = json.loads(row["lifecycle_detail"])
    assert detail["failed_phase"] == "startup_recovery"
    assert detail["retryable"] is True
    assert "harness_starting" in detail["reason"]


@pytest.mark.asyncio
async def test_leaves_running_rows_alone(db, monkeypatch):
    _seed("auto-healthy", None)
    await _run_sweep(monkeypatch, alive=set())
    row = _row("auto-healthy")
    assert row["startup_state"] is None
    assert row["state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_leaves_sticky_failed_rows_alone(db, monkeypatch):
    _seed("auto-failed", "setup_failed")
    await _run_sweep(monkeypatch, alive=set())
    row = _row("auto-failed")
    assert row["startup_state"] == "setup_failed"
    assert row["state"] == "FAILED"  # untouched — visibility preserved


@pytest.mark.asyncio
async def test_sweep_survives_per_row_errors(db, monkeypatch):
    """One bad row must not abort recovery of the rest."""
    from tools.dashboard import server

    _seed("auto-bad", "setup_running")
    _seed("auto-good", "setup_running")
    _backdate("auto-bad")
    _backdate("auto-good")

    calls = []

    def _exists(name):
        calls.append(name)
        if name == "auto-bad":
            raise RuntimeError("tmux exploded")
        return True

    monkeypatch.setattr(server, "_tmux_session_exists", _exists)
    server.session_monitor._event_bus = AsyncMock()
    server.session_monitor._event_bus.broadcast = AsyncMock()
    await server._recover_stuck_lifecycle_rows()

    assert _row("auto-good")["startup_state"] is None
    assert set(calls) == {"auto-bad", "auto-good"}


# ── a launch still within its step budget (auto-2btus) ──


@pytest.mark.asyncio
async def test_a_launch_within_its_budget_is_left_launching(db, monkeypatch):
    """Hot reload, run 12: the sweep ran at 02:45:55 while the old worker was
    mid-launch; its tmux appeared at 02:45:57 and the session came up. The
    sweep must not fail it."""
    from tools.dashboard import server

    _seed("auto-inflight", "launching_container")
    scheduled = []
    monkeypatch.setattr(server, "_recheck_launch_after_budget",
                        lambda name, seen, delay: scheduled.append((name, delay)) or _noop())
    await _run_sweep(monkeypatch, alive=set())
    row = _row("auto-inflight")
    assert row["state"] == "LAUNCHING"
    assert row["startup_state"] == "launching_container"
    [(name, delay)] = scheduled
    assert name == "auto-inflight"
    # launching_container's budget is 60s; the re-check comes just after it
    # and inside the reaper's belt (budget + 60s).
    assert 60 < delay <= 60 + server._STARTUP_RECOVERY_RECHECK_SLACK_S + 1


async def _noop():
    return None


@pytest.mark.asyncio
async def test_recheck_leaves_a_launch_that_moved_on(db, monkeypatch):
    from tools.dashboard import server
    from tools.dashboard.session_lifecycle_worker import STATE_AUTHORITY

    _seed("auto-moved", "launching_container")
    seen = dashboard_db.get_session("auto-moved")
    _backdate("auto-moved", 1)   # the worker's next transition stamps a new time
    STATE_AUTHORITY.transition("auto-moved", "LAUNCHING", phase="harness_starting", cause="test")
    monkeypatch.setattr(server, "_tmux_session_exists", lambda name: False)
    await server._recheck_launch_after_budget("auto-moved", seen, 0)
    assert _row("auto-moved")["state"] == "LAUNCHING"
    assert _row("auto-moved")["startup_state"] == "harness_starting"


@pytest.mark.asyncio
async def test_recheck_resolves_an_unchanged_launch(db, monkeypatch):
    from tools.dashboard import server

    _seed("auto-orphan", "launching_container")
    _seed("auto-survivor", "harness_starting")
    seen_orphan = dashboard_db.get_session("auto-orphan")
    seen_survivor = dashboard_db.get_session("auto-survivor")
    monkeypatch.setattr(server, "_tmux_session_exists", lambda name: name == "auto-survivor")
    server.session_monitor._event_bus = AsyncMock()
    server.session_monitor._event_bus.broadcast = AsyncMock()
    await server._recheck_launch_after_budget("auto-orphan", seen_orphan, 0)
    await server._recheck_launch_after_budget("auto-survivor", seen_survivor, 0)
    orphan = _row("auto-orphan")
    assert orphan["state"] == "FAILED"
    assert json.loads(orphan["lifecycle_detail"])["failed_phase"] == "startup_recovery"
    assert _row("auto-survivor")["state"] == "ACTIVE"
