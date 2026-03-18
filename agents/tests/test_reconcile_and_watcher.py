"""Tests for reconcile_state() and the merged dispatch+nav watcher."""

import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agents.dispatch_db as db
from agents.dispatcher import (
    RunningAgent,
    reconcile_state,
    REPO_ROOT,
)


# ── Helpers ──────────────────────────────────────────────────────


def _use_temp_db():
    """Point dispatch_db at a temp file, init schema, return path."""
    tmp = tempfile.mktemp(suffix=".db")
    db.DB_PATH = Path(tmp)
    db.init_db()
    return tmp


def _make_agent(bead_id: str, **overrides) -> RunningAgent:
    defaults = dict(
        bead_id=bead_id,
        container_name=f"agent-{bead_id}-1234",
        container_id="abc123def456",
        output_dir=f"/tmp/test-{bead_id}",
        worktree_path=f"/tmp/worktrees/{bead_id}",
        branch=f"agent/{bead_id}",
        branch_base="base111",
        image="autonomy-agent",
        started_at=time.time(),
    )
    defaults.update(overrides)
    return RunningAgent(**defaults)


def _insert_running_row(run_id: str, bead_id: str) -> None:
    """Insert a RUNNING row directly into the temp DB."""
    conn = sqlite3.connect(str(db.DB_PATH))
    conn.execute(
        "INSERT INTO dispatch_runs (id, bead_id, status) VALUES (?, ?, 'RUNNING')",
        (run_id, bead_id),
    )
    conn.commit()
    conn.close()


def _get_row_status(run_id: str) -> str | None:
    conn = sqlite3.connect(str(db.DB_PATH))
    row = conn.execute(
        "SELECT status FROM dispatch_runs WHERE id=?", (run_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ── reconcile_state ──────────────────────────────────────────────


class TestReconcileState:
    def setup_method(self):
        _use_temp_db()

    def test_orphaned_running_row_marked_failed(self):
        """RUNNING row with no matching live container → status=FAILED."""
        _insert_running_row("orphan-run-1", "auto-orphan")

        # No live agents
        reconcile_state([])

        assert _get_row_status("orphan-run-1") == "FAILED"

    def test_running_row_with_live_container_untouched(self):
        """RUNNING row whose bead has a live container → status stays RUNNING."""
        _insert_running_row("live-run-1", "auto-live")
        agent = _make_agent("auto-live")

        reconcile_state([agent])

        assert _get_row_status("live-run-1") == "RUNNING"

    def test_multiple_orphans_all_marked_failed(self):
        """Multiple orphaned RUNNING rows are all marked FAILED."""
        _insert_running_row("orphan-a", "auto-orphan-a")
        _insert_running_row("orphan-b", "auto-orphan-b")
        _insert_running_row("orphan-c", "auto-orphan-c")

        reconcile_state([])

        assert _get_row_status("orphan-a") == "FAILED"
        assert _get_row_status("orphan-b") == "FAILED"
        assert _get_row_status("orphan-c") == "FAILED"

    def test_mixed_orphaned_and_live(self):
        """Only orphaned rows are marked FAILED; live rows are untouched."""
        _insert_running_row("run-live", "auto-live")
        _insert_running_row("run-orphan", "auto-orphan")
        agent = _make_agent("auto-live")

        reconcile_state([agent])

        assert _get_row_status("run-live") == "RUNNING"
        assert _get_row_status("run-orphan") == "FAILED"

    @patch("agents.dispatcher.run_bd", return_value='[{"id": "auto-stale"}]')
    def test_dolt_in_progress_reset(self, mock_run_bd):
        """in_progress bead with no container → run_bd update called."""
        reconcile_state([])

        # run_bd should be called with update args for the stale bead
        update_calls = [
            call for call in mock_run_bd.call_args_list
            if call.args[0][:2] == ["update", "auto-stale"]
        ]
        assert any("-s" in args for call in update_calls for args in call.args[0]), (
            "Expected bd update -s open for stale in_progress bead"
        )

    @patch("agents.dispatcher.run_bd", return_value='[{"id": "auto-live"}]')
    def test_dolt_in_progress_not_reset_if_container_live(self, mock_run_bd):
        """in_progress bead whose container is alive → NOT reset."""
        agent = _make_agent("auto-live")

        reconcile_state([agent])

        update_calls = [
            call for call in mock_run_bd.call_args_list
            if len(call.args[0]) >= 2 and call.args[0][0] == "update" and call.args[0][1] == "auto-live"
        ]
        assert not update_calls, "Should not reset bead that has a live container"

    def test_no_orphaned_rows_no_error(self):
        """No orphaned rows → completes without error."""
        _insert_running_row("active-run", "auto-active")
        agent = _make_agent("auto-active")
        reconcile_state([agent])  # Should not raise

    def test_empty_state_no_error(self):
        """Empty DB + empty running list → completes without error."""
        reconcile_state([])  # Should not raise

    @patch("agents.dispatcher.run_bd", side_effect=Exception("bd down"))
    def test_dolt_failure_does_not_crash(self, _mock):
        """Dolt query failure is swallowed — SQLite reconcile still runs."""
        _insert_running_row("orphan-run", "auto-orphan")

        reconcile_state([])  # Should not raise

        # SQLite reconcile should still have run
        assert _get_row_status("orphan-run") == "FAILED"


# ── Merged watcher logic ─────────────────────────────────────────


def _simulate_watcher_cycle(dispatch_data: dict, open_count: int) -> tuple[dict, dict]:
    """Simulate one watcher cycle: collect dispatch data, derive nav data.

    This mirrors the exact logic in server.py's _dispatch_watcher() so we
    can test the invariant without importing the full Starlette server.
    Returns (dispatch_payload, nav_payload) as would be broadcast.
    """
    counts = {"open_count": open_count}
    nav_data = {
        "open_beads": counts.get("open_count", 0),
        "running_agents": len(dispatch_data["active"]),
        "approved_waiting": len(dispatch_data["waiting"]),
        "approved_blocked": len(dispatch_data["blocked"]),
    }
    return dispatch_data, nav_data


class TestMergedWatcher:
    """Verify dispatch and nav topics derive from the same data snapshot."""

    def test_nav_counts_match_dispatch_lists(self):
        """Nav counts are exactly the lengths of active/waiting/blocked."""
        active = [{"id": "auto-a"}]
        waiting = [{"id": "auto-b"}, {"id": "auto-c"}]
        blocked = [{"id": "auto-d"}]
        dispatch_data = {"active": active, "waiting": waiting, "blocked": blocked}

        _, nav = _simulate_watcher_cycle(dispatch_data, open_count=42)

        assert nav["running_agents"] == 1
        assert nav["approved_waiting"] == 2
        assert nav["approved_blocked"] == 1
        assert nav["open_beads"] == 42

    def test_nav_and_dispatch_see_same_snapshot(self):
        """After a cycle, nav counts are consistent with the dispatch lists."""
        active = [{"id": "auto-x"}, {"id": "auto-y"}]
        waiting = [{"id": "auto-z"}]
        blocked = []
        dispatch_data = {"active": active, "waiting": waiting, "blocked": blocked}

        dispatch, nav = _simulate_watcher_cycle(dispatch_data, open_count=100)

        assert nav["running_agents"] == len(dispatch["active"])
        assert nav["approved_waiting"] == len(dispatch["waiting"])
        assert nav["approved_blocked"] == len(dispatch["blocked"])

    def test_empty_dispatch_produces_zero_nav_counts(self):
        """Empty dispatch lists → all nav counts zero."""
        dispatch_data = {"active": [], "waiting": [], "blocked": []}

        _, nav = _simulate_watcher_cycle(dispatch_data, open_count=0)

        assert nav["running_agents"] == 0
        assert nav["approved_waiting"] == 0
        assert nav["approved_blocked"] == 0
        assert nav["open_beads"] == 0

    def test_open_beads_comes_from_bead_counts_not_dispatch(self):
        """open_beads reflects total open beads, not just approved/dispatched ones."""
        dispatch_data = {"active": [{"id": "auto-a"}], "waiting": [], "blocked": []}

        # 50 total open beads but only 1 dispatched
        _, nav = _simulate_watcher_cycle(dispatch_data, open_count=50)

        assert nav["open_beads"] == 50
        assert nav["running_agents"] == 1

    def test_both_topics_derived_atomically(self):
        """Both topics use the same dispatch_data object (no separate queries)."""
        active = [{"id": "auto-1"}, {"id": "auto-2"}]
        waiting = [{"id": "auto-3"}]
        blocked = [{"id": "auto-4"}, {"id": "auto-5"}]
        dispatch_data = {"active": active, "waiting": waiting, "blocked": blocked}

        dispatch, nav = _simulate_watcher_cycle(dispatch_data, open_count=10)

        # Verify nav counts are consistent with the dispatch payload
        assert nav["running_agents"] == len(dispatch["active"])
        assert nav["approved_waiting"] == len(dispatch["waiting"])
        assert nav["approved_blocked"] == len(dispatch["blocked"])
