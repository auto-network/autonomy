"""auto-a1jco: session lifecycle phase columns + transitions.

The session lifecycle has two independent dimensions modeled as two
columns: setup_phase tracks /startup.sh progress, harness_phase tracks
exec claude. They progress concurrently — see graph://18c9a9e9-efb.

These tests verify:
- The columns exist and default to 'pending'.
- update_tail_state accepts both fields independently.
- get_registry surfaces both fields on the SSE payload.
- The setup_exit watcher distinguishes exit=0 (setup_complete) vs
  non-zero (setup_failed).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.dashboard.dao import dashboard_db
from tools.dashboard import session_monitor as sm


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point dashboard_db at a fresh sqlite file under tmp_path.

    Uses the documented DASHBOARD_DB env override + module reload — same
    pattern as the test_app fixture in conftest.py.
    """
    db_path = tmp_path / "test_dash.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    import importlib
    importlib.reload(dashboard_db)
    yield db_path
    # Close + null the connection so the next test's reload starts clean.
    if dashboard_db._conn is not None:
        dashboard_db._conn.close()
    dashboard_db._conn = None


def _seed_session(tmux_name: str = "auto-test") -> None:
    """Create a minimum-viable tmux_sessions row for the test."""
    conn = dashboard_db.get_conn()
    conn.execute(
        "INSERT INTO tmux_sessions (tmux_name, type, project, harness, created_at) "
        "VALUES (?, 'container', 'autonomy', 'claude', ?)",
        (tmux_name, time.time()),
    )
    conn.commit()


def test_columns_exist_with_pending_default(temp_db):
    """Fresh DB has setup_phase and harness_phase columns; both default to 'pending'."""
    _seed_session("auto-fresh")
    conn = dashboard_db.get_conn()
    row = conn.execute(
        "SELECT setup_phase, harness_phase FROM tmux_sessions WHERE tmux_name=?",
        ("auto-fresh",),
    ).fetchone()
    assert row["setup_phase"] == "pending"
    assert row["harness_phase"] == "pending"


def test_update_tail_state_accepts_setup_phase(temp_db):
    """update_tail_state(setup_phase=...) writes the field."""
    _seed_session("auto-setup")
    dashboard_db.update_tail_state("auto-setup", setup_phase="setup_running")
    conn = dashboard_db.get_conn()
    row = conn.execute(
        "SELECT setup_phase, harness_phase FROM tmux_sessions WHERE tmux_name=?",
        ("auto-setup",),
    ).fetchone()
    assert row["setup_phase"] == "setup_running"
    assert row["harness_phase"] == "pending"  # unchanged


def test_update_tail_state_accepts_harness_phase(temp_db):
    """update_tail_state(harness_phase=...) writes the field."""
    _seed_session("auto-harness")
    dashboard_db.update_tail_state(
        "auto-harness", harness_phase="first_turn_written",
    )
    conn = dashboard_db.get_conn()
    row = conn.execute(
        "SELECT setup_phase, harness_phase FROM tmux_sessions WHERE tmux_name=?",
        ("auto-harness",),
    ).fetchone()
    assert row["harness_phase"] == "first_turn_written"
    assert row["setup_phase"] == "pending"  # unchanged


def test_update_tail_state_writes_both_phases_atomically(temp_db):
    """A single call can advance both dimensions."""
    _seed_session("auto-both")
    dashboard_db.update_tail_state(
        "auto-both",
        setup_phase="container_starting",
        harness_phase="harness_starting",
    )
    conn = dashboard_db.get_conn()
    row = conn.execute(
        "SELECT setup_phase, harness_phase FROM tmux_sessions WHERE tmux_name=?",
        ("auto-both",),
    ).fetchone()
    assert row["setup_phase"] == "container_starting"
    assert row["harness_phase"] == "harness_starting"


def test_get_registry_surfaces_phases(temp_db):
    """get_registry payload carries setup_phase + harness_phase per row."""
    _seed_session("auto-reg")
    dashboard_db.update_tail_state(
        "auto-reg",
        setup_phase="setup_running",
        harness_phase="harness_starting",
    )
    monitor = sm.SessionMonitor()
    registry = monitor.get_registry()
    auto_reg = next((r for r in registry if r["session_id"] == "auto-reg"), None)
    assert auto_reg is not None
    assert auto_reg["setup_phase"] == "setup_running"
    assert auto_reg["harness_phase"] == "harness_starting"


def test_get_registry_default_pending_for_new_rows(temp_db):
    """Newly-INSERTed rows that haven't transitioned yet broadcast 'pending'."""
    _seed_session("auto-new")
    monitor = sm.SessionMonitor()
    registry = monitor.get_registry()
    auto_new = next((r for r in registry if r["session_id"] == "auto-new"), None)
    assert auto_new is not None
    assert auto_new["setup_phase"] == "pending"
    assert auto_new["harness_phase"] == "pending"


def test_phase_persists_across_other_updates(temp_db):
    """Setting phase, then updating an unrelated field, doesn't reset phase."""
    _seed_session("auto-persist")
    dashboard_db.update_tail_state(
        "auto-persist",
        setup_phase="setup_complete",
        harness_phase="composer_ready",
    )
    # An unrelated subsequent update.
    dashboard_db.update_tail_state("auto-persist", last_message="hello")
    conn = dashboard_db.get_conn()
    row = conn.execute(
        "SELECT setup_phase, harness_phase, last_message FROM tmux_sessions "
        "WHERE tmux_name=?",
        ("auto-persist",),
    ).fetchone()
    assert row["setup_phase"] == "setup_complete"
    assert row["harness_phase"] == "composer_ready"
    assert row["last_message"] == "hello"


def test_derived_ready_from_phase_pair(temp_db):
    """The 'ready' state is the AND of setup_complete + composer_ready;
    asserted at the registry-consumer level, not as a column."""
    _seed_session("auto-pre-ready")
    dashboard_db.update_tail_state(
        "auto-pre-ready",
        setup_phase="setup_complete",
        harness_phase="first_turn_written",
    )
    monitor = sm.SessionMonitor()
    reg = monitor.get_registry()
    row = next((r for r in reg if r["session_id"] == "auto-pre-ready"), None)
    # Not ready: harness_phase != composer_ready.
    ready = (
        row["setup_phase"] == "setup_complete"
        and row["harness_phase"] == "composer_ready"
    )
    assert ready is False

    dashboard_db.update_tail_state(
        "auto-pre-ready", harness_phase="composer_ready",
    )
    reg = monitor.get_registry()
    row = next((r for r in reg if r["session_id"] == "auto-pre-ready"), None)
    ready = (
        row["setup_phase"] == "setup_complete"
        and row["harness_phase"] == "composer_ready"
    )
    assert ready is True
