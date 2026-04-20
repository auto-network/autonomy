"""auto-8bnq0 Defect 2 — reconcile deregisters stale dispatch rows.

When a dispatch container exits but the dispatcher's deregister POST fails
or the dispatcher is mid-restart, tmux_sessions retains is_live=1 with no
matching RUNNING dispatch_runs entry. auto-opbyh's liveness-loop type
filter (correctly) leaves dispatch rows alone — their death is
dispatcher-owned. Nothing else sweeps them. Over time, dashboard.db
accumulates stale 'live' dispatch rows (observed 2026-04-20 on s2ep5).

New contract: on dispatcher startup, iterate tmux_sessions rows with
type IN ('dispatch','librarian') AND is_live=1; for each row where no
dispatch_runs entry with status='RUNNING' matches the tmux_name, POST
/api/monitor/deregister. Idempotent.

FAIL-REASON on master: no code sweeps these rows. Test expects deregister
HTTP call to fire for the stale row; master makes zero deregister calls
at startup.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .conftest import (
    fetch_tmux_row,
    insert_dispatch_run,
    insert_tmux_session,
)


class TestReconcileStaleDispatchRows:
    """Defect 2 — reconcile deregisters orphaned tmux_sessions rows."""

    def test_reconcile_deregisters_stale_dispatch_row(self, cleanup_env):
        """is_live=1 dispatch row with no matching RUNNING run → POST deregister."""
        dispatcher = cleanup_env["dispatcher"]
        dashboard_db = cleanup_env["dashboard_db"]

        stale_tmux = "auto-stale-disp-20260420-100000"
        # Plant a stale is_live=1 dispatch row
        sess_dir = cleanup_env["tmp_path"] / "sessions"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / f"{stale_tmux}.jsonl"
        jsonl.write_text("")
        insert_tmux_session(
            dashboard_db, tmux_name=stale_tmux, type_="dispatch",
            jsonl_path=str(jsonl), is_live=1, bead_id="auto-stale",
        )
        # Crucially: NO corresponding RUNNING dispatch_runs entry

        assert hasattr(dispatcher, "reconcile_stale_monitor_rows"), (
            "agents.dispatcher.reconcile_stale_monitor_rows missing — "
            "Defect 2 not yet implemented. Expected callable that POSTs "
            "/api/monitor/deregister for every is_live=1 dispatch or "
            "librarian row with no matching RUNNING dispatch_runs entry."
        )

        # Capture HTTP calls via the dispatcher's _monitor_post helper
        captured_posts = []

        def capture_post(path, body, *, tmux_name=None):
            captured_posts.append({"path": path, "body": body, "tmux_name": tmux_name})

        with patch.object(dispatcher, "_monitor_post", capture_post):
            dispatcher.reconcile_stale_monitor_rows()

        # Assert: deregister was POSTed for the stale row
        dereg_calls = [
            c for c in captured_posts
            if "/api/monitor/deregister" in c["path"]
            and c.get("body", {}).get("tmux_name") == stale_tmux
        ]
        assert len(dereg_calls) == 1, (
            f"Expected exactly one deregister POST for {stale_tmux!r}; got "
            f"{len(dereg_calls)}. Posts: {captured_posts!r}"
        )

    def test_reconcile_does_not_touch_live_dispatch(self, cleanup_env):
        """Live RUNNING dispatch with alive container → untouched.

        Regression guard — reconcile must not deregister a legitimately
        running dispatch just because the startup pass happens to see it.
        """
        dispatcher = cleanup_env["dispatcher"]
        dashboard_db = cleanup_env["dashboard_db"]
        dispatch_db = cleanup_env["dispatch_db"]

        live_tmux = "auto-live-disp-20260420-100000"
        live_bead = "auto-live"
        container = f"agent-{live_bead}-999"

        sess_dir = cleanup_env["tmp_path"] / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        jsonl = sess_dir / f"{live_tmux}.jsonl"
        jsonl.write_text("")
        insert_tmux_session(
            dashboard_db, tmux_name=live_tmux, type_="dispatch",
            jsonl_path=str(jsonl), is_live=1, bead_id=live_bead,
        )
        # MATCHING RUNNING dispatch_runs entry
        insert_dispatch_run(
            dispatch_db, run_id=live_tmux, bead_id=live_bead,
            status="RUNNING", container_name=container,
        )

        assert hasattr(dispatcher, "reconcile_stale_monitor_rows"), (
            "reconcile_stale_monitor_rows missing."
        )

        captured_posts = []

        def capture_post(path, body, *, tmux_name=None):
            captured_posts.append({"path": path, "body": body, "tmux_name": tmux_name})

        # Simulate container alive
        with patch.object(dispatcher, "_running_agent_containers",
                          return_value={container}, create=True), \
             patch.object(dispatcher, "_monitor_post", capture_post):
            dispatcher.reconcile_stale_monitor_rows()

        dereg_calls = [
            c for c in captured_posts
            if "/api/monitor/deregister" in c["path"]
            and c.get("body", {}).get("tmux_name") == live_tmux
        ]
        assert len(dereg_calls) == 0, (
            f"Reconcile deregistered a LIVE dispatch {live_tmux!r} — must "
            "only sweep rows with no matching RUNNING dispatch_runs entry. "
            f"Posts: {captured_posts!r}"
        )

        # Assert the row is still live in the DB
        row = fetch_tmux_row(dashboard_db, live_tmux)
        assert row is not None and row["is_live"] == 1, (
            f"Live dispatch row was flipped to dead: {row!r}"
        )
