"""Tests for dispatcher bd error handling — returncode checks, retries, cleanup flags."""

from unittest.mock import patch, call
import subprocess
import time

import pytest

from agents.dispatcher import (
    BdCommandError,
    run_bd,
    run_cmd,
    _retry_bd,
    release_bead,
    claim_bead,
    REPO_ROOT,
)


def _completed_process(stdout="", stderr="", returncode=0):
    """Create a mock CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ── run_bd ──────────────────────────────────────────────────────


class TestRunBd:
    @patch("agents.dispatcher.subprocess.run")
    def test_success_returns_stdout(self, mock_run):
        mock_run.return_value = _completed_process(stdout="ok\n")
        assert run_bd(["show", "auto-1"]) == "ok"

    @patch("agents.dispatcher.subprocess.run")
    def test_nonzero_exit_returns_empty(self, mock_run):
        mock_run.return_value = _completed_process(
            returncode=1, stderr="dolt unreachable"
        )
        assert run_bd(["update", "auto-1", "-s", "open"]) == ""

    @patch("agents.dispatcher.subprocess.run")
    def test_nonzero_exit_logs_stderr(self, mock_run, capsys):
        mock_run.return_value = _completed_process(
            returncode=1, stderr="connection refused"
        )
        run_bd(["set-state", "auto-1", "work=claimed"])
        captured = capsys.readouterr()
        assert "set-state failed" in captured.err
        assert "connection refused" in captured.err

    @patch("agents.dispatcher.subprocess.run")
    def test_check_true_raises_on_failure(self, mock_run):
        mock_run.return_value = _completed_process(
            returncode=1, stderr="not found"
        )
        with pytest.raises(BdCommandError) as exc_info:
            run_bd(["close", "auto-1"], check=True)
        assert exc_info.value.returncode == 1
        assert "not found" in exc_info.value.stderr

    @patch("agents.dispatcher.subprocess.run")
    def test_check_true_raises_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=15)
        with pytest.raises(BdCommandError) as exc_info:
            run_bd(["query", "all"], check=True)
        assert exc_info.value.returncode == -1

    @patch("agents.dispatcher.subprocess.run")
    def test_check_false_returns_empty_on_failure(self, mock_run):
        """Default check=False returns empty string, not exception."""
        mock_run.return_value = _completed_process(
            returncode=1, stderr="err"
        )
        result = run_bd(["update", "auto-1", "-s", "open"])
        assert result == ""

    @patch("agents.dispatcher.subprocess.run")
    def test_success_with_check_returns_stdout(self, mock_run):
        mock_run.return_value = _completed_process(stdout="done")
        result = run_bd(["close", "auto-1"], check=True)
        assert result == "done"


# ── run_cmd ─────────────────────────────────────────────────────


class TestRunCmd:
    @patch("agents.dispatcher.subprocess.run")
    def test_success_returns_stdout(self, mock_run):
        mock_run.return_value = _completed_process(stdout="result")
        assert run_cmd(["graph", "search", "test"]) == "result"

    @patch("agents.dispatcher.subprocess.run")
    def test_nonzero_exit_returns_empty(self, mock_run):
        mock_run.return_value = _completed_process(
            returncode=1, stderr="graph error"
        )
        assert run_cmd(["graph", "search", "test"]) == ""

    @patch("agents.dispatcher.subprocess.run")
    def test_nonzero_exit_logs_stderr(self, mock_run, capsys):
        mock_run.return_value = _completed_process(
            returncode=2, stderr="db locked"
        )
        run_cmd(["graph", "search", "test"])
        captured = capsys.readouterr()
        assert "graph failed" in captured.err
        assert "db locked" in captured.err


# ── _retry_bd ───────────────────────────────────────────────────


class TestRetryBd:
    @patch("agents.dispatcher.time.sleep")
    @patch("agents.dispatcher.subprocess.run")
    def test_succeeds_first_try(self, mock_run, mock_sleep):
        mock_run.return_value = _completed_process(stdout="ok")
        result = _retry_bd(["set-state", "auto-1", "work=claimed"])
        assert result == "ok"
        mock_sleep.assert_not_called()

    @patch("agents.dispatcher.time.sleep")
    @patch("agents.dispatcher.subprocess.run")
    def test_succeeds_after_retry(self, mock_run, mock_sleep):
        """Fails first, succeeds on second attempt."""
        mock_run.side_effect = [
            _completed_process(returncode=1, stderr="transient"),
            _completed_process(stdout="ok"),
        ]
        result = _retry_bd(["update", "auto-1", "-s", "open"], max_retries=2)
        assert result == "ok"
        mock_sleep.assert_called_once_with(1)  # 2^0 = 1s

    @patch("agents.dispatcher.time.sleep")
    @patch("agents.dispatcher.subprocess.run")
    def test_all_retries_exhausted_raises(self, mock_run, mock_sleep):
        """All attempts fail — raises BdCommandError."""
        mock_run.return_value = _completed_process(
            returncode=1, stderr="persistent error"
        )
        with pytest.raises(BdCommandError):
            _retry_bd(["close", "auto-1"], max_retries=2)
        # Should have waited twice: 1s, 2s
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @patch("agents.dispatcher.time.sleep")
    @patch("agents.dispatcher.subprocess.run")
    def test_logs_critical_on_exhaustion(self, mock_run, mock_sleep, capsys):
        mock_run.return_value = _completed_process(
            returncode=1, stderr="down"
        )
        with pytest.raises(BdCommandError):
            _retry_bd(["set-state", "auto-1", "dispatch=done"], max_retries=1)
        captured = capsys.readouterr()
        assert "CRITICAL" in captured.err


# ── release_bead ────────────────────────────────────────────────


class TestReleaseBead:
    @patch("agents.dispatcher._retry_bd")
    def test_done_closes_bead(self, mock_retry):
        """DONE status calls bd close with retry."""
        mock_retry.return_value = "ok"

        result = release_bead("auto-1", "DONE", "completed task")

        assert result is True
        mock_retry.assert_called_once_with(
            ["close", "auto-1", "--reason", "completed task"]
        )

    @patch("agents.dispatcher.run_bd")
    @patch("agents.dispatcher._retry_bd")
    def test_failed_resets_and_flags(self, mock_retry, mock_run_bd):
        """FAILED status resets to open and appends notes."""
        mock_retry.return_value = "ok"
        mock_run_bd.return_value = ""

        result = release_bead("auto-1", "FAILED", "agent crashed")

        assert result is True
        assert mock_retry.call_count == 1  # update only (no set-state)
        mock_retry.assert_called_once_with(["update", "auto-1", "-s", "open"])
        mock_run_bd.assert_called_once_with(
            ["update", "auto-1", "--append-notes", "Failed: agent crashed"]
        )

    @patch("agents.dispatcher._retry_bd")
    def test_retry_failure_returns_false(self, mock_retry):
        """If critical mutations fail after retries, returns False."""
        mock_retry.side_effect = BdCommandError(["close"], 1, "down")

        result = release_bead("auto-1", "DONE", "done")

        assert result is False

    @patch("agents.dispatcher._retry_bd")
    def test_stale_bead_warning_logged(self, mock_retry, capsys):
        """On failure, logs STALE BEAD WARNING with bead ID."""
        mock_retry.side_effect = BdCommandError(["close"], 1, "down")

        release_bead("auto-1", "DONE", "done")

        captured = capsys.readouterr()
        assert "STALE BEAD WARNING" in captured.err
        assert "auto-1" in captured.err

    @patch("agents.dispatcher.run_bd")
    @patch("agents.dispatcher._retry_bd")
    def test_blocked_status_handling(self, mock_retry, mock_run_bd):
        """BLOCKED status resets to open and appends notes."""
        mock_retry.return_value = "ok"
        mock_run_bd.return_value = ""

        result = release_bead("auto-1", "BLOCKED", "merge conflict")

        assert result is True
        mock_retry.assert_called_once_with(["update", "auto-1", "-s", "open"])
        mock_run_bd.assert_called_once_with(
            ["update", "auto-1", "--append-notes", "Blocked: merge conflict"]
        )


# ── claim_bead ──────────────────────────────────────────────────


class TestClaimBead:
    def test_returns_true(self):
        """claim_bead always returns True; RUNNING row is written by _record_launch."""
        result = claim_bead("auto-1")
        assert result is True
