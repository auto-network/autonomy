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
    assert row["startup_state"] is None
    assert row["attention"] == "idle"  # tracker-domain value, matches birth
    assert row["ended_at"] is None


def test_terminal_entry_stamps_ended_at_and_clears_attention(db):
    _seed(state="ACTIVE")
    w = SessionLifecycleStateWriter()
    assert w.transition("auto-t", "ENDED", cause="test") is True
    row = _row()
    assert row["state"] == "ENDED"
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


def test_failed_carries_detail_and_sticky_chip(db):
    _seed(state="LAUNCHING")
    w = SessionLifecycleStateWriter()
    assert w.transition(
        "auto-t", "FAILED", cause="test",
        reason="boom", failed_phase="setup", retryable=True, attempt=2,
    ) is True
    row = _row()
    assert row["state"] == "FAILED"
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
    assert row["ended_at"] is not None


def test_mark_dead_on_launching_row_keeps_tuple_consistent(db):
    """LAUNCHING→ENDED is legal (the worker's own stop flow uses it); NOT
    reaping LAUNCHING rows is the reaper's own state check (Phase B). This
    pins the authority-level guarantee: whatever a caller does, the
    persisted tuple stays consistent."""
    _seed(state="LAUNCHING")
    dashboard_db.mark_dead("auto-t")
    row = _row()
    assert row["state"] == "ENDED"
    assert row["ended_at"] is not None


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
    assert json.loads(row["lifecycle_detail"])["reason"] == "boom"


def test_update_activity_state_only_touches_active_rows(db):
    _seed(state="ACTIVE")
    dashboard_db.update_activity_state("auto-t", "tool_running")
    assert _row()["attention"] == "tool_running"

    dashboard_db.mark_dead("auto-t")
    dashboard_db.update_activity_state("auto-t", "idle")
    row = _row()
    # A trailing tracker batch must not punch telemetry into a dead row.
    assert row["state"] == "ENDED"
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


def test_migration_backfill_equals_derive_fallback(tmp_path, monkeypatch):
    """Run the REAL migration against a legacy-schema DB covering the
    legacy tuple space and assert the backfilled state equals
    derive_lifecycle_state's fallback for every row — the two definitions
    must never drift."""
    import itertools
    import sqlite3 as sq
    import time as _time

    from tools.dashboard.session_lifecycle_worker import derive_lifecycle_state

    db_path = tmp_path / "legacy.db"
    conn = sq.connect(str(db_path))
    conn.execute(
        "CREATE TABLE tmux_sessions ("
        " tmux_name TEXT PRIMARY KEY, type TEXT NOT NULL,"
        " project TEXT NOT NULL, jsonl_path TEXT, bead_id TEXT,"
        " session_uuid TEXT, created_at REAL NOT NULL,"
        " is_live INTEGER DEFAULT 1, activity_state TEXT DEFAULT 'idle',"
        " startup_state TEXT, last_activity REAL)"
    )
    tuples = list(itertools.product(
        ["failed", "stopping", "cleaning", "dead", "idle", "running",
         "tool_running", "thinking", None],
        [0, 1],
        [None, "setup_failed", "setup_running", "requesting"],
    ))
    expected = {}
    for i, (activity, live, startup) in enumerate(tuples):
        name = f"auto-tuple-{i}"
        conn.execute(
            "INSERT INTO tmux_sessions"
            " (tmux_name, type, project, created_at, is_live,"
            "  activity_state, startup_state, last_activity)"
            " VALUES (?, 'container', 'x', ?, ?, ?, ?, ?)",
            (name, _time.time(), live, activity, startup, _time.time()),
        )
        expected[name] = derive_lifecycle_state({
            "activity_state": activity,
            "is_live": live,
            "startup_state": startup,
        })
    conn.commit()
    conn.close()

    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    prior = getattr(dashboard_db, "_conn", None)
    if prior is not None:
        try:
            prior.close()
        except Exception:
            pass
    dashboard_db._conn = None  # type: ignore[attr-defined]
    dashboard_db.init_db(db_path)  # the real migration: backfill + drop

    conn = sq.connect(str(db_path))
    conn.row_factory = sq.Row
    rows = conn.execute("SELECT tmux_name, state FROM tmux_sessions").fetchall()
    mismatches = {
        r["tmux_name"]: (r["state"], expected[r["tmux_name"]])
        for r in rows if r["state"] != expected[r["tmux_name"]]
    }
    assert not mismatches, f"backfill != derive for: {mismatches}"
    # And the legacy columns are gone.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tmux_sessions)")}
    assert "is_live" not in cols and "activity_state" not in cols
    conn.close()
    dashboard_db._conn = None  # type: ignore[attr-defined]


def test_migration_backfill_tolerates_partial_legacy_schema(tmp_path, monkeypatch):
    """A table can predate some of the legacy columns entirely (very old
    DBs; hand-rolled test fixtures). The backfill must introspect what
    exists and apply absent-value semantics — is_live present without
    activity_state/startup_state backfills live rows to ACTIVE and dead
    rows to ENDED instead of raising ``no such column``."""
    import sqlite3 as sq
    import time as _time

    db_path = tmp_path / "partial.db"
    conn = sq.connect(str(db_path))
    conn.execute(
        "CREATE TABLE tmux_sessions ("
        " tmux_name TEXT PRIMARY KEY, type TEXT NOT NULL,"
        " project TEXT NOT NULL, jsonl_path TEXT, bead_id TEXT,"
        " session_uuid TEXT, created_at REAL NOT NULL,"
        " is_live INTEGER DEFAULT 1, last_activity REAL)"
    )
    now = _time.time()
    conn.execute(
        "INSERT INTO tmux_sessions (tmux_name, type, project, created_at,"
        " is_live, last_activity) VALUES ('auto-live', 'container', 'x', ?, 1, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO tmux_sessions (tmux_name, type, project, created_at,"
        " is_live, last_activity) VALUES ('auto-dead', 'container', 'x', ?, 0, ?)",
        (now, now),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    prior = getattr(dashboard_db, "_conn", None)
    if prior is not None:
        try:
            prior.close()
        except Exception:
            pass
    dashboard_db._conn = None  # type: ignore[attr-defined]
    dashboard_db.init_db(db_path)

    conn = sq.connect(str(db_path))
    conn.row_factory = sq.Row
    states = {
        r["tmux_name"]: r["state"]
        for r in conn.execute("SELECT tmux_name, state FROM tmux_sessions")
    }
    assert states == {"auto-live": "ACTIVE", "auto-dead": "ENDED"}
    conn.close()
    dashboard_db._conn = None  # type: ignore[attr-defined]
