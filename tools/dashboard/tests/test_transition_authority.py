"""The transition authority — the single writer of session state.

Contract (FSM consolidation, graph://92ed929a-3ec): one closed-set
``state`` column (LAUNCHING/ACTIVE/STOPPING/ENDED/FAILED) written only by
``SessionLifecycleStateWriter.transition``, which validates the move
against the legality matrix and stamps the legacy write-through
projections (startup_state/activity_state/is_live), last_activity,
ended_at, attention, and lifecycle_detail in ONE atomic UPDATE — so a
contradictory tuple (alive-and-dead, dead-and-still-setting-up) is not
representable through any code path.
"""
from __future__ import annotations

import json

import pytest

from tools.dashboard.dao import dashboard_db
from tools.dashboard.session_lifecycle_worker import (
    STATE_AUTHORITY,
    SessionLifecycleStateWriter,
)


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


def _row(name="auto-t"):
    return dashboard_db.get_session(name)


def _seed(name="auto-t", state="LAUNCHING"):
    dashboard_db.insert_session(
        tmux_name=name, session_type="container", project="x", state=state,
    )


# ── Births ───────────────────────────────────────────────────────────


def test_rows_born_active_carry_consistent_projections(db):
    _seed(state="ACTIVE")
    row = _row()
    assert row["state"] == "ACTIVE"
    assert row["is_live"] == 1
    assert row["attention"] == "idle"


def test_rows_born_launching_have_no_attention(db):
    _seed(state="LAUNCHING")
    row = _row()
    assert row["state"] == "LAUNCHING"
    assert row["attention"] is None


# ── The atomic write ─────────────────────────────────────────────────


def test_transition_stamps_state_and_projections_together(db):
    _seed(state="LAUNCHING")
    w = SessionLifecycleStateWriter()
    assert w.transition("auto-t", "ACTIVE", cause="test") is True
    row = _row()
    assert row["state"] == "ACTIVE"
    assert row["is_live"] == 1
    assert row["startup_state"] is None
    assert row["activity_state"] == "running"  # legacy projection value
    assert row["attention"] == "idle"  # tracker-domain value, matches birth
    assert row["ended_at"] is None


def test_terminal_entry_stamps_ended_at_and_clears_attention(db):
    _seed(state="ACTIVE")
    w = SessionLifecycleStateWriter()
    assert w.transition("auto-t", "ENDED", cause="test") is True
    row = _row()
    assert row["state"] == "ENDED"
    assert row["is_live"] == 0
    assert row["activity_state"] == "dead"
    assert row["ended_at"] is not None
    assert row["attention"] is None


def test_reentry_clears_ended_at(db):
    _seed(state="ACTIVE")
    w = SessionLifecycleStateWriter()
    w.transition("auto-t", "ENDED", cause="test")
    assert w.transition("auto-t", "LAUNCHING", phase="requesting", cause="test") is True
    row = _row()
    assert row["ended_at"] is None
    assert row["startup_state"] == "requesting"
    assert row["is_live"] == 1


def test_failed_carries_detail_and_sticky_chip(db):
    _seed(state="LAUNCHING")
    w = SessionLifecycleStateWriter()
    assert w.transition(
        "auto-t", "FAILED", cause="test",
        reason="boom", failed_phase="setup", retryable=True, attempt=2,
    ) is True
    row = _row()
    assert row["state"] == "FAILED"
    assert row["is_live"] == 0
    assert row["startup_state"] == "setup_failed"
    assert row["ended_at"] is not None
    detail = json.loads(row["lifecycle_detail"])
    assert detail["failed_phase"] == "setup"
    assert detail["attempt"] == 2


# ── Legality ─────────────────────────────────────────────────────────


def test_illegal_transition_refused_and_row_untouched(db):
    _seed(state="ACTIVE")
    w = SessionLifecycleStateWriter()
    before = dict(_row())
    assert w.transition("auto-t", "LAUNCHING", phase="requesting", cause="test") is False
    assert dict(_row()) == before


def test_failed_never_becomes_ended(db):
    _seed(state="LAUNCHING")
    w = SessionLifecycleStateWriter()
    w.transition("auto-t", "FAILED", cause="test", reason="x", failed_phase="setup")
    assert w.transition("auto-t", "ENDED", cause="test") is False
    assert _row()["state"] == "FAILED"


@pytest.mark.parametrize("terminal", ["ENDED", "FAILED"])
def test_relaunch_enters_from_terminal(db, terminal):
    _seed(state="LAUNCHING")
    w = SessionLifecycleStateWriter()
    if terminal == "FAILED":
        w.transition("auto-t", "FAILED", cause="test", reason="x", failed_phase="s")
    else:
        w.transition("auto-t", "ACTIVE", cause="test")
        w.transition("auto-t", "ENDED", cause="test")
    assert w.transition("auto-t", "LAUNCHING", phase="requesting", cause="retry") is True
    assert _row()["state"] == "LAUNCHING"


# ── Routed causes ────────────────────────────────────────────────────


def test_mark_dead_routes_through_authority(db):
    _seed(state="ACTIVE")
    dashboard_db.mark_dead("auto-t")
    row = _row()
    assert row["state"] == "ENDED"
    assert row["is_live"] == 0
    assert row["activity_state"] == "dead"
    assert row["ended_at"] is not None


def test_mark_dead_on_launching_row_keeps_tuple_consistent(db):
    """LAUNCHING→ENDED is legal (the worker's own stop flow uses it); NOT
    reaping LAUNCHING rows is the reaper's own state check (Phase B). This
    pins the authority-level guarantee: whatever a caller does, the
    persisted tuple stays consistent."""
    _seed(state="LAUNCHING")
    dashboard_db.mark_dead("auto-t")
    row = _row()
    assert (row["state"] == "ENDED") == (row["is_live"] == 0)
    assert row["activity_state"] == "dead"


def test_failed_cleanup_deregister_keeps_failed_terminal(db):
    """The FAILED-session cleanup unregisters WITHOUT recording death —
    the row stays FAILED with its retryable detail; ENDED→FAILED is not in
    the matrix and must never be needed."""
    import asyncio
    from tools.dashboard.session_monitor import SessionMonitor

    _seed(state="LAUNCHING")
    w = SessionLifecycleStateWriter()
    w.transition("auto-t", "FAILED", cause="test", reason="boom", failed_phase="setup")

    m = SessionMonitor()
    asyncio.get_event_loop_policy().new_event_loop()
    asyncio.run(m.deregister("auto-t", record_death=False))
    row = _row()
    assert row["state"] == "FAILED"
    assert row["is_live"] == 0
    assert json.loads(row["lifecycle_detail"])["reason"] == "boom"


def test_update_activity_state_only_touches_active_rows(db):
    _seed(state="ACTIVE")
    dashboard_db.update_activity_state("auto-t", "tool_running")
    assert _row()["attention"] == "tool_running"
    assert _row()["activity_state"] == "tool_running"

    dashboard_db.mark_dead("auto-t")
    dashboard_db.update_activity_state("auto-t", "idle")
    row = _row()
    # A trailing tracker batch must not punch telemetry into a dead row.
    assert row["state"] == "ENDED"
    assert row["activity_state"] == "dead"
    assert row["attention"] is None


def test_shared_authority_is_the_worker_default(db):
    from tools.dashboard.session_lifecycle_worker import SessionLifecycleWorker

    assert SessionLifecycleWorker()._state_writer is STATE_AUTHORITY


# ── Domains are schema-enforced ──────────────────────────────────────


def test_state_domain_is_check_enforced(db):
    """An out-of-domain state is unrepresentable — the schema rejects it,
    no matter what code path attempts the write."""
    import sqlite3

    _seed(state="ACTIVE")
    conn = dashboard_db.get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE tmux_sessions SET state='RUNNING' WHERE tmux_name='auto-t'",
        )


def test_attention_domain_is_check_enforced(db):
    import sqlite3

    _seed(state="ACTIVE")
    conn = dashboard_db.get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE tmux_sessions SET attention='running' WHERE tmux_name='auto-t'",
        )
