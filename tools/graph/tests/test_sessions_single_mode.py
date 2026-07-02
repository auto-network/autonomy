"""Tests for `graph sessions --session`/bare-self-default/`--all` (W4, auto-gah4g).

Host-mode (direct, non-HttpClient) coverage — the container/HTTP-client
path is covered in tools/dashboard/tests/test_ingest_mutex.py's
TestSingleSessionMode. conftest's autouse fixture pins
_FORCE_HOST_DIRECT=True, so these tests exercise cmd_sessions' direct
branch without needing a live dashboard.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from tools.graph import cli as graph_cli
from tools.graph.db import resolve_caller_db_path


def _args(**kw) -> argparse.Namespace:
    defaults = {
        "db": resolve_caller_db_path(None),
        "status": False, "since": None, "topics": False,
        "session": None, "all": False, "project": None, "force": False,
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


class TestExplicitSessionFlag:
    def test_session_flag_resolves_and_ingests_only_that_file(self, capsys):
        fake_row = {"jsonl_path": "/tmp/fake-session.jsonl"}
        fake_result = {"status": "ingested", "new_thoughts": 3, "new_derivations": 2}

        with patch("tools.dashboard.dao.dashboard_db.get_session", return_value=fake_row), \
             patch("tools.graph.ingest._open_db_for_session") as mock_open_db, \
             patch("tools.graph.cli.ingest_session_file", return_value=fake_result) as mock_ingest:
            mock_db = mock_open_db.return_value
            graph_cli.cmd_sessions(_args(session="auto-explicit"))

        mock_ingest.assert_called_once()
        called_path = mock_ingest.call_args[0][1]
        assert called_path == Path("/tmp/fake-session.jsonl")
        mock_db.close.assert_called_once()
        out = capsys.readouterr().out
        assert "auto-explicit" in out
        assert "ingested" in out

    def test_missing_jsonl_path_exits_nonzero(self, capsys):
        with patch("tools.dashboard.dao.dashboard_db.get_session", return_value={"jsonl_path": None}):
            try:
                graph_cli.cmd_sessions(_args(session="auto-nopath"))
                assert False, "expected SystemExit"
            except SystemExit as e:
                assert e.code != 0


class TestBareDefaultsToSelf:
    def test_bare_with_autonomy_session_set_ingests_self(self, capsys, monkeypatch):
        monkeypatch.setenv("AUTONOMY_SESSION", "auto-self-test")
        fake_row = {"jsonl_path": "/tmp/self.jsonl"}
        fake_result = {"status": "ingested", "new_thoughts": 1, "new_derivations": 1}

        with patch("tools.dashboard.dao.dashboard_db.get_session", return_value=fake_row) as mock_get, \
             patch("tools.graph.ingest._open_db_for_session") as mock_open_db, \
             patch("tools.graph.cli.ingest_session_file", return_value=fake_result):
            graph_cli.cmd_sessions(_args())

        mock_get.assert_called_once_with("auto-self-test")

    def test_bare_without_autonomy_session_falls_back_to_project(self, capsys, monkeypatch):
        """Host/CI invocations (no AUTONOMY_SESSION) must keep the old
        project-scan default — only container sessions get the new
        self-targeting behavior."""
        monkeypatch.delenv("AUTONOMY_SESSION", raising=False)
        with patch("tools.graph.cli.ingest_claude_code_project", return_value=[]) as mock_project, \
             patch("tools.dashboard.dao.dashboard_db.get_session") as mock_get:
            graph_cli.cmd_sessions(_args())

        mock_project.assert_called_once()
        mock_get.assert_not_called()

    def test_explicit_all_overrides_autonomy_session(self, capsys, monkeypatch):
        monkeypatch.setenv("AUTONOMY_SESSION", "auto-self-test")
        with patch("tools.graph.cli.catch_up_sweep", return_value={
            "scanned": 0, "changed": 0, "unchanged": 0, "sealed": 0,
            "db_opens": 0, "orgs_touched": 0, "results": [],
        }) as mock_sweep, \
             patch("tools.dashboard.dao.dashboard_db.get_session") as mock_get:
            graph_cli.cmd_sessions(_args(all=True))

        mock_sweep.assert_called_once()
        mock_get.assert_not_called()

    def test_explicit_project_overrides_autonomy_session(self, capsys, monkeypatch):
        monkeypatch.setenv("AUTONOMY_SESSION", "auto-self-test")
        with patch("tools.graph.cli.ingest_claude_code_project", return_value=[]) as mock_project, \
             patch("tools.dashboard.dao.dashboard_db.get_session") as mock_get:
            graph_cli.cmd_sessions(_args(project="/some/project"))

        mock_project.assert_called_once()
        mock_get.assert_not_called()


class TestAllRoutesThroughCatchUpSweep:
    def test_all_flag_calls_catch_up_sweep_not_legacy_full_reparse(self, capsys, monkeypatch):
        monkeypatch.delenv("AUTONOMY_SESSION", raising=False)
        sweep_stats = {
            "scanned": 5, "changed": 2, "unchanged": 3, "sealed": 0,
            "db_opens": 1, "orgs_touched": 1, "results": [],
        }
        with patch("tools.graph.cli.catch_up_sweep", return_value=sweep_stats) as mock_sweep, \
             patch("tools.graph.cli.ingest_all_claude_code") as mock_legacy:
            graph_cli.cmd_sessions(_args(all=True, force=True))

        mock_sweep.assert_called_once_with(force=True)
        mock_legacy.assert_not_called()
        out = capsys.readouterr().out
        assert "scanned 5" in out
        assert "changed 2" in out
