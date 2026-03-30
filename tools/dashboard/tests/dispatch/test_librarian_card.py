"""Tests for librarian dispatch card fixes.

L1: Unit tests for dispatch_cmd.py librarian handling (no crash on empty bead_id).
L2.A: HTTP contract tests for librarian_type in API response.
L2.B: Behavioral browser sweep for librarian card rendering.

Run: pytest tools/dashboard/tests/dispatch/test_librarian_card.py -v
"""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.dashboard.tests import fixtures


# ── L1: Unit tests for dispatch_cmd.py ────────────────────────────────

class TestDispatchWatchLibrarianL1:
    """dispatch watch must not crash when a librarian run has empty bead_id."""

    def _make_runs(self, *, bead_id="", dir_name="librarian-review_report-abc-20260330-120000",
                   status="RUNNING", librarian_type="review_report"):
        """Build a mock API response with a librarian run."""
        return [
            {
                "bead_id": bead_id,
                "dir": dir_name,
                "id": dir_name,
                "status": status,
                "librarian_type": librarian_type,
                "duration_secs": 60,
            },
        ]

    def test_watch_no_crash_librarian_running(self):
        """graph dispatch watch with a running librarian does not crash."""
        from tools.graph import dispatch_cmd

        runs = self._make_runs()
        call_count = [0]

        def mock_api_call(base, path, ctx):
            call_count[0] += 1
            if call_count[0] == 1:
                return runs
            # Second call: librarian completed
            return [{**runs[0], "status": "DONE", "completed_at": "2026-03-30T12:05:00Z"}]

        with patch.object(dispatch_cmd, "_api_call", side_effect=mock_api_call), \
             patch.object(dispatch_cmd, "_get_dashboard_url", return_value="https://localhost:8080"), \
             patch.object(dispatch_cmd, "_make_ssl_ctx", return_value=None), \
             patch("time.sleep"):  # skip actual sleeping
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                args = MagicMock()
                args.timeout = 5
                args.interval = 1
                dispatch_cmd.cmd_dispatch_watch(args)

            output = buf.getvalue()
            # Should print the dir name, not crash
            assert "librarian-review_report-abc-20260330-120000" in output

    def test_watch_running_ids_uses_dir_fallback(self):
        """Running IDs set uses dir as fallback when bead_id is empty."""
        from tools.graph import dispatch_cmd

        runs = self._make_runs()

        def mock_api_call(base, path, ctx):
            return runs

        with patch.object(dispatch_cmd, "_api_call", side_effect=mock_api_call), \
             patch.object(dispatch_cmd, "_get_dashboard_url", return_value="https://localhost:8080"), \
             patch.object(dispatch_cmd, "_make_ssl_ctx", return_value=None), \
             patch("time.sleep", side_effect=KeyboardInterrupt):  # exit after first poll
            buf = io.StringIO()
            with patch("sys.stdout", buf), \
                 pytest.raises(KeyboardInterrupt):
                args = MagicMock()
                args.timeout = 300
                args.interval = 1
                dispatch_cmd.cmd_dispatch_watch(args)

            output = buf.getvalue()
            # Should mention 1 running dispatch with dir name
            assert "1 running" in output
            assert "librarian-review_report-abc-20260330-120000" in output

    def test_watch_seen_completed_uses_dir_fallback(self):
        """Completed librarian runs use dir as fallback for seen_completed set."""
        from tools.graph import dispatch_cmd

        runs = [
            {
                "bead_id": "",
                "dir": "librarian-review_report-abc-20260330-110000",
                "id": "librarian-review_report-abc-20260330-110000",
                "status": "DONE",
                "completed_at": "2026-03-30T11:05:00Z",
                "librarian_type": "review_report",
            },
        ]

        def mock_api_call(base, path, ctx):
            return runs

        with patch.object(dispatch_cmd, "_api_call", side_effect=mock_api_call), \
             patch.object(dispatch_cmd, "_get_dashboard_url", return_value="https://localhost:8080"), \
             patch.object(dispatch_cmd, "_make_ssl_ctx", return_value=None):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                args = MagicMock()
                args.timeout = 5
                args.interval = 1
                dispatch_cmd.cmd_dispatch_watch(args)

            output = buf.getvalue()
            # No running dispatches — should say so
            assert "No running dispatch" in output


class TestDispatchRunsLibrarianL1:
    """dispatch runs must show librarian type in output."""

    def test_runs_shows_librarian_type(self):
        """graph dispatch runs with librarian run shows type label."""
        from tools.graph import dispatch_cmd

        runs = [
            {
                "bead_id": "librarian-review_report-abc-20260330-120000",
                "dir": "librarian-review_report-abc-20260330-120000",
                "status": "DONE",
                "librarian_type": "review_report",
                "duration_secs": 45,
                "decision": {"status": "DONE", "reason": "Review complete"},
            },
        ]

        def mock_api_call(base, path, ctx):
            return runs

        with patch.object(dispatch_cmd, "_api_call", side_effect=mock_api_call), \
             patch.object(dispatch_cmd, "_get_dashboard_url", return_value="https://localhost:8080"), \
             patch.object(dispatch_cmd, "_make_ssl_ctx", return_value=None):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                args = MagicMock()
                args.limit = 20
                args.json = False
                args.primer = False
                args.running = False
                args.failed = False
                args.completed = False
                dispatch_cmd.cmd_dispatch_runs(args)

            output = buf.getvalue()
            assert "librarian:review_report" in output

    def test_runs_regular_bead_not_affected(self):
        """Regular bead runs still show bead_id."""
        from tools.graph import dispatch_cmd

        runs = [
            {
                "bead_id": "auto-test1",
                "dir": "auto-test1-20260330-120000",
                "status": "DONE",
                "librarian_type": None,
                "duration_secs": 300,
                "decision": {"status": "DONE", "reason": "All tests pass"},
            },
        ]

        def mock_api_call(base, path, ctx):
            return runs

        with patch.object(dispatch_cmd, "_api_call", side_effect=mock_api_call), \
             patch.object(dispatch_cmd, "_get_dashboard_url", return_value="https://localhost:8080"), \
             patch.object(dispatch_cmd, "_make_ssl_ctx", return_value=None):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                args = MagicMock()
                args.limit = 20
                args.json = False
                args.primer = False
                args.running = False
                args.failed = False
                args.completed = False
                dispatch_cmd.cmd_dispatch_runs(args)

            output = buf.getvalue()
            assert "auto-test1" in output
            assert "librarian" not in output


# ── L2.A: HTTP contract tests ────────────────────────────────────────

def _make_librarian_fixture():
    """Build a mock fixture with a librarian run."""
    return {
        "active_sessions": fixtures.STANDARD_SESSIONS,
        "beads": [
            {"id": "auto-reg1", "title": "Regular bead", "priority": 1,
             "status": "open", "labels": []},
        ],
        "runs": [
            {
                "id": "librarian-review_report-abc-20260330-120000",
                "bead_id": "",
                "dir": "librarian-review_report-abc-20260330-120000",
                "status": "DONE",
                "started_at": "2026-03-30T12:00:00Z",
                "completed_at": "2026-03-30T12:02:00Z",
                "duration_secs": 120,
                "librarian_type": "review_report",
            },
            {
                "id": "auto-reg1-20260330-110000",
                "bead_id": "auto-reg1",
                "dir": "auto-reg1-20260330-110000",
                "status": "DONE",
                "started_at": "2026-03-30T11:00:00Z",
                "completed_at": "2026-03-30T11:05:00Z",
                "duration_secs": 300,
            },
        ],
        "experiments": [fixtures.make_experiment(fixtures.TEST_EXPERIMENT_ID)],
    }


@pytest.fixture
def librarian_client(tmp_path):
    """Boot a mock dashboard with librarian run data, return TestClient."""
    import importlib

    fixture_path = tmp_path / "fixtures.json"
    fixture_path.write_text(json.dumps(_make_librarian_fixture()))

    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")

    env_patches = {
        "DASHBOARD_MOCK": str(fixture_path),
        "DASHBOARD_MOCK_EVENTS": str(events_path),
    }

    with patch.dict(os.environ, env_patches):
        from tools.dashboard.dao import mock as mock_mod
        importlib.reload(mock_mod)
        from tools.dashboard import server as srv_mod
        importlib.reload(srv_mod)
        from starlette.testclient import TestClient
        with TestClient(srv_mod.app) as client:
            yield client


class TestDispatchRunsAPILibrarian:
    """L2.A: GET /api/dispatch/runs includes librarian_type field."""

    def test_librarian_run_has_librarian_type(self, librarian_client):
        """Librarian run in response includes librarian_type field."""
        resp = librarian_client.get("/api/dispatch/runs")
        assert resp.status_code == 200
        runs = resp.json()

        librarian_runs = [r for r in runs if r.get("librarian_type")]
        assert len(librarian_runs) >= 1, f"No librarian runs found in {runs}"

        lib_run = librarian_runs[0]
        assert lib_run["librarian_type"] == "review_report"

    def test_librarian_run_has_nonempty_identifier(self, librarian_client):
        """Librarian run has a non-empty bead_id (uses dir name as fallback)."""
        resp = librarian_client.get("/api/dispatch/runs")
        runs = resp.json()

        librarian_runs = [r for r in runs if r.get("librarian_type")]
        assert len(librarian_runs) >= 1

        lib_run = librarian_runs[0]
        assert lib_run.get("bead_id"), f"Librarian run has empty bead_id: {lib_run}"

    def test_librarian_run_has_title(self, librarian_client):
        """Librarian run has a synthetic title field."""
        resp = librarian_client.get("/api/dispatch/runs")
        runs = resp.json()

        librarian_runs = [r for r in runs if r.get("librarian_type")]
        lib_run = librarian_runs[0]
        assert lib_run.get("title"), f"Librarian run has no title: {lib_run}"
        assert "Librarian" in lib_run["title"]

    def test_regular_run_unaffected(self, librarian_client):
        """Regular bead run is not affected by librarian changes."""
        resp = librarian_client.get("/api/dispatch/runs")
        runs = resp.json()

        regular_runs = [r for r in runs if not r.get("librarian_type")]
        assert len(regular_runs) >= 1

        reg_run = regular_runs[0]
        assert reg_run["bead_id"] == "auto-reg1"
        assert reg_run.get("librarian_type") is None
