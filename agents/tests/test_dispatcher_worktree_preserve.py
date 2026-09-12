"""Tests for bead-dispatch worktree preservation across retries (auto-cpxdp).

A bead dispatch that times out or exits without a decision must NOT have its
worktree force-removed: the worktree lives on the persistent host bind-mount
and holds any UNCOMMITTED work from the interrupted run. The next dispatch
reuses the same tree (launch.sh reuse path) instead of discarding the work.

Covers:
- resolve_bead_worktree: DONE tears down, retryable outcomes preserve, the
  retry cap tears down a perpetually-failing bead's tree.
- _bead_preserve_retry_count / _bead_is_closed helpers.
- process_decision no-decision branch preserves rather than removes.
- reconcile_state does not reap a dirty worktree of an open bead (real git),
  but does reap it once the bead is terminal, and still reaps clean orphans.
"""

import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import agents.dispatch_db as db
from agents.dispatcher import (
    DispatchResult,
    MAX_PRESERVE_RETRIES,
    resolve_bead_worktree,
    _bead_preserve_retry_count,
    _bead_is_closed,
    process_decision,
    reconcile_state,
)


# ── Helpers ──────────────────────────────────────────────────────


def _use_temp_db():
    tmp = tempfile.mktemp(suffix=".db")
    db.DB_PATH = Path(tmp)
    db.init_db()
    return tmp


def _insert_run(run_id: str, bead_id: str, status: str, started_at: str) -> None:
    conn = sqlite3.connect(str(db.DB_PATH))
    conn.execute(
        "INSERT INTO dispatch_runs (id, bead_id, status, started_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, bead_id, status, started_at, started_at),
    )
    conn.commit()
    conn.close()


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _make_repo_with_worktree(root: Path, bead_id: str, dirty: bool):
    """Create a git repo at root with a worktree on agent/<bead_id>.

    Returns (worktree_path, branch_base_sha). When dirty=True the worktree has
    an uncommitted change; HEAD stays at branch_base (no new commits).
    """
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hello\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                          capture_output=True, text=True).stdout.strip()

    wt = root / ".worktrees" / f"{bead_id}-20260101-000000"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "-b", f"agent/{bead_id}", str(wt), "master")

    if dirty:
        (wt / "work.py").write_text("# uncommitted work from interrupted run\n")

    # branch_base sidecar the reconcile path reads
    run_dir = root / "data" / "agent-runs" / f"{bead_id}-20260101-000000"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ".branch_base").write_text(base)
    return wt, base


# ── resolve_bead_worktree ────────────────────────────────────────


class TestResolveBeadWorktree:
    @patch("agents.dispatcher.cleanup_worktree")
    def test_done_tears_down(self, mock_cleanup, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        resolve_bead_worktree(str(wt), "DONE", "auto-x")
        mock_cleanup.assert_called_once_with(str(wt))

    @pytest.mark.parametrize("status", ["FAILED", "TIMEOUT", "BLOCKED", "MERGE_FAILED"])
    @patch("agents.dispatcher._bead_preserve_retry_count", return_value=0)
    @patch("agents.dispatcher.cleanup_worktree")
    def test_retryable_preserves(self, mock_cleanup, _count, status, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        resolve_bead_worktree(str(wt), status, "auto-x")
        mock_cleanup.assert_not_called()
        assert wt.exists()

    @patch("agents.dispatcher._bead_preserve_retry_count",
           return_value=MAX_PRESERVE_RETRIES)
    @patch("agents.dispatcher.cleanup_worktree")
    def test_retry_cap_tears_down(self, mock_cleanup, _count, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        resolve_bead_worktree(str(wt), "FAILED", "auto-x")
        mock_cleanup.assert_called_once_with(str(wt))

    @patch("agents.dispatcher.cleanup_worktree")
    def test_missing_path_is_noop(self, mock_cleanup, tmp_path):
        resolve_bead_worktree(str(tmp_path / "nope"), "FAILED", "auto-x")
        mock_cleanup.assert_not_called()


# ── retry-count / closed helpers ─────────────────────────────────


class TestPreserveRetryCount:
    def setup_method(self):
        _use_temp_db()

    def test_counts_non_done_since_last_done(self):
        _insert_run("r1", "auto-y", "DONE", "2026-01-01 00:00:00")
        _insert_run("r2", "auto-y", "FAILED", "2026-01-02 00:00:00")
        _insert_run("r3", "auto-y", "TIMEOUT", "2026-01-03 00:00:00")
        assert _bead_preserve_retry_count("auto-y") == 2

    def test_resets_at_done(self):
        _insert_run("r1", "auto-z", "FAILED", "2026-01-01 00:00:00")
        _insert_run("r2", "auto-z", "DONE", "2026-01-02 00:00:00")
        assert _bead_preserve_retry_count("auto-z") == 0

    def test_running_rows_skipped(self):
        _insert_run("r1", "auto-w", "FAILED", "2026-01-01 00:00:00")
        _insert_run("r2", "auto-w", "RUNNING", "2026-01-02 00:00:00")
        assert _bead_preserve_retry_count("auto-w") == 1

    def test_empty_is_zero(self):
        assert _bead_preserve_retry_count("auto-none") == 0


class TestBeadIsClosed:
    @patch("agents.dispatcher.run_bd", return_value='[{"id": "auto-a", "status": "closed"}]')
    def test_closed(self, _bd):
        assert _bead_is_closed("auto-a") is True

    @patch("agents.dispatcher.run_bd", return_value='[{"id": "auto-a", "status": "open"}]')
    def test_open(self, _bd):
        assert _bead_is_closed("auto-a") is False

    @patch("agents.dispatcher.run_bd", side_effect=Exception("bd down"))
    def test_error_favors_preserve(self, _bd):
        assert _bead_is_closed("auto-a") is False


# ── process_decision no-decision branch ──────────────────────────


class TestProcessDecisionNoDecision:
    @patch("agents.dispatcher.resolve_bead_worktree")
    @patch("agents.dispatcher.cleanup_worktree")
    @patch("agents.dispatcher.release_bead")
    @patch("agents.dispatcher.run_bd")
    def test_no_decision_preserves(self, _bd, _release, mock_cleanup, mock_resolve):
        result = DispatchResult(
            bead_id="auto-nd", exit_code=1, decision=None,
            worktree_path="/tmp/wt-nd", branch="agent/auto-nd",
        )
        status = process_decision(result)
        assert status == "FAILED"
        # Goes through the status-aware resolver, NOT a blind force-remove.
        mock_resolve.assert_called_once_with("/tmp/wt-nd", "FAILED", "auto-nd")
        mock_cleanup.assert_not_called()


# ── reconcile_state worktree handling (real git) ─────────────────


class TestReconcilePreservesDirtyWorktree:
    def setup_method(self):
        _use_temp_db()

    @patch("agents.dispatcher.run_bd", return_value='[{"id": "auto-open", "status": "open"}]')
    def test_dirty_open_bead_preserved(self, _bd, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        wt, _base = _make_repo_with_worktree(root, "auto-open", dirty=True)
        monkeypatch.setattr("agents.dispatcher.REPO_ROOT", root)
        monkeypatch.setattr("agents.dispatcher.DATA_ROOT", root / "data")

        reconcile_state([])

        assert wt.exists(), "dirty worktree of an open bead must be preserved"
        assert (wt / "work.py").exists(), "uncommitted work must survive"

    @patch("agents.dispatcher.run_bd", return_value='[{"id": "auto-clean", "status": "open"}]')
    def test_clean_orphan_still_reaped(self, _bd, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        wt, _base = _make_repo_with_worktree(root, "auto-clean", dirty=False)
        monkeypatch.setattr("agents.dispatcher.REPO_ROOT", root)
        monkeypatch.setattr("agents.dispatcher.DATA_ROOT", root / "data")

        reconcile_state([])

        assert not wt.exists(), "clean orphan with no commits should be removed"

    @patch("agents.dispatcher.run_bd", return_value='[{"id": "auto-done", "status": "closed"}]')
    def test_dirty_closed_bead_reaped(self, _bd, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        wt, _base = _make_repo_with_worktree(root, "auto-done", dirty=True)
        monkeypatch.setattr("agents.dispatcher.REPO_ROOT", root)
        monkeypatch.setattr("agents.dispatcher.DATA_ROOT", root / "data")

        reconcile_state([])

        assert not wt.exists(), "dirty worktree of a closed/abandoned bead should be reaped"
