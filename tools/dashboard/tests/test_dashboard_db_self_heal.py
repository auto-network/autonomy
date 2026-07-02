"""Tests for dashboard_db connection self-healing (W6, auto-4uvpx).

2026-07-02 incident: the module-level ``_conn`` had no invalidation path,
so a closed/corrupted handle made every subsequent get_conn() call fail
identically until the dashboard process was restarted — liveness and
reconcile ticks failed every tick, log-only, with no operator visibility.
``get_conn()`` now probes the connection and rebuilds on failure.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def fresh_ddb(tmp_path, monkeypatch):
    """Reload dashboard_db bound to a scratch DB path."""
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    yield ddb


class TestConnSelfHeal:
    def test_get_conn_self_heals_after_close(self, fresh_ddb):
        """A closed connection is transparently rebuilt on the next call —
        no restart, no manual intervention."""
        ddb = fresh_ddb
        conn1 = ddb.get_conn()
        conn1.execute(
            "INSERT INTO tmux_sessions (tmux_name, type, project, created_at)"
            " VALUES ('probe', 'host', 'autonomy', 0)"
        )
        conn1.commit()
        conn1.close()  # simulate the handle going bad

        conn2 = ddb.get_conn()
        assert conn2 is not conn1
        # New connection is functional and sees prior committed data.
        row = conn2.execute(
            "SELECT tmux_name FROM tmux_sessions WHERE tmux_name='probe'"
        ).fetchone()
        assert row is not None

    def test_get_conn_survives_repeated_breakage(self, fresh_ddb):
        """Closing the handle N times in a row keeps healing — the fix
        isn't a one-shot retry, it's durable across the process lifetime."""
        ddb = fresh_ddb
        for _ in range(3):
            conn = ddb.get_conn()
            conn.close()
        conn = ddb.get_conn()
        conn.execute("SELECT 1")  # would raise ProgrammingError if still broken

    def test_reset_conn_forces_rebuild_on_next_get(self, fresh_ddb):
        """reset_conn() is available as a direct invalidation hook (used by
        other error paths that detect a bad handle out-of-band)."""
        ddb = fresh_ddb
        conn1 = ddb.get_conn()
        ddb.reset_conn()
        conn2 = ddb.get_conn()
        assert conn2 is not conn1
        conn2.execute("SELECT 1")

    def test_healthy_connection_is_reused(self, fresh_ddb):
        """The common case: no probe failure → same connection object,
        no needless reconnect churn."""
        ddb = fresh_ddb
        conn1 = ddb.get_conn()
        conn2 = ddb.get_conn()
        assert conn1 is conn2
