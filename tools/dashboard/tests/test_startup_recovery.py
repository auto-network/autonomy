"""Startup recovery sweep (_recover_stuck_lifecycle_rows).

FSM contract, correctness addition 3: the lifecycle queue is process
memory, so a dashboard restart mid-launch strands rows in a non-terminal
startup_state with is_live=1. The boot sweep must adopt rows whose tmux
session survived (clear startup_state) and fail rows whose process is
gone (failed(startup_recovery), retryable), while leaving healthy and
sticky-failed rows untouched.
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
    dashboard_db.insert_session(
        tmux_name=tmux_name, session_type="container", project="x",
    )
    if startup_state is not None:
        conn = dashboard_db.get_conn()
        conn.execute(
            "UPDATE tmux_sessions SET startup_state=? WHERE tmux_name=?",
            (startup_state, tmux_name),
        )
        conn.commit()


def _row(tmux_name: str) -> dict:
    conn = dashboard_db.get_conn()
    cur = conn.execute(
        "SELECT startup_state, activity_state, is_live, lifecycle_detail"
        " FROM tmux_sessions WHERE tmux_name=?",
        (tmux_name,),
    )
    row = cur.fetchone()
    keys = ("startup_state", "activity_state", "is_live", "lifecycle_detail")
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
    await _run_sweep(monkeypatch, alive={"auto-stuck"})
    row = _row("auto-stuck")
    assert row["startup_state"] is None
    assert row["is_live"] == 1


@pytest.mark.asyncio
async def test_fails_row_when_tmux_gone(db, monkeypatch):
    _seed("auto-zombie", "harness_starting")
    await _run_sweep(monkeypatch, alive=set())
    row = _row("auto-zombie")
    assert row["startup_state"] == "setup_failed"
    assert row["activity_state"] == "failed"
    assert row["is_live"] == 0
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
    assert row["is_live"] == 1
    assert row["activity_state"] != "failed"


@pytest.mark.asyncio
async def test_leaves_sticky_failed_rows_alone(db, monkeypatch):
    _seed("auto-failed", "setup_failed")
    await _run_sweep(monkeypatch, alive=set())
    row = _row("auto-failed")
    assert row["startup_state"] == "setup_failed"
    assert row["is_live"] == 1  # untouched — visibility preserved as-is


@pytest.mark.asyncio
async def test_sweep_survives_per_row_errors(db, monkeypatch):
    """One bad row must not abort recovery of the rest."""
    from tools.dashboard import server

    _seed("auto-bad", "setup_running")
    _seed("auto-good", "setup_running")

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
