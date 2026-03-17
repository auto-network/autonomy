"""Tests for dispatcher dirty working tree handling before merge."""

from unittest.mock import MagicMock, patch, call
import subprocess

import pytest

from agents.dispatcher import (
    check_working_tree_clean,
    merge_branch,
    REPO_ROOT,
)


def _completed_process(stdout="", stderr="", returncode=0):
    """Create a mock CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ── check_working_tree_clean ────────────────────────────────────


class TestCheckWorkingTreeClean:
    @patch("agents.dispatcher.subprocess.run")
    def test_clean_tree(self, mock_run):
        mock_run.return_value = _completed_process(stdout="")
        is_clean, summary = check_working_tree_clean()
        assert is_clean is True
        assert summary == ""
        mock_run.assert_called_once_with(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )

    @patch("agents.dispatcher.subprocess.run")
    def test_dirty_tree_single_file(self, mock_run):
        mock_run.return_value = _completed_process(stdout=" M app.js\n")
        is_clean, summary = check_working_tree_clean()
        assert is_clean is False
        assert "app.js" in summary

    @patch("agents.dispatcher.subprocess.run")
    def test_dirty_tree_multiple_files(self, mock_run):
        files = "\n".join([f" M file{i}.py" for i in range(15)])
        mock_run.return_value = _completed_process(stdout=files)
        is_clean, summary = check_working_tree_clean()
        assert is_clean is False
        assert "and 5 more" in summary

    @patch("agents.dispatcher.subprocess.run")
    def test_untracked_files(self, mock_run):
        mock_run.return_value = _completed_process(stdout="?? new_file.txt\n")
        is_clean, summary = check_working_tree_clean()
        assert is_clean is False
        assert "new_file.txt" in summary


# ── merge_branch ────────────────────────────────────────────────


class TestMergeBranch:
    @patch("agents.dispatcher.subprocess.run")
    def test_clean_tree_successful_merge(self, mock_run):
        """Clean tree + successful merge = (True, "")."""
        mock_run.side_effect = [
            # check_working_tree_clean: git status --porcelain
            _completed_process(stdout=""),
            # git merge
            _completed_process(stdout="Merge made"),
        ]
        ok, err = merge_branch("agent/auto-xyz", "auto-xyz", "fix bug")
        assert ok is True
        assert err == ""

    @patch("agents.dispatcher.subprocess.run")
    def test_clean_tree_merge_conflict(self, mock_run):
        """Clean tree + merge conflict = (False, "Merge conflict: ...")."""
        mock_run.side_effect = [
            # check_working_tree_clean: git status --porcelain
            _completed_process(stdout=""),
            # git merge (fails)
            _completed_process(returncode=1, stderr="CONFLICT (content): Merge conflict in foo.py"),
            # git merge --abort
            _completed_process(),
        ]
        ok, err = merge_branch("agent/auto-xyz", "auto-xyz", "fix bug")
        assert ok is False
        assert "Merge conflict" in err
        assert "foo.py" in err

    @patch("agents.dispatcher.subprocess.run")
    def test_dirty_tree_stash_merge_restore(self, mock_run):
        """Dirty tree + stash succeeds + merge succeeds + stash pop succeeds."""
        mock_run.side_effect = [
            # check_working_tree_clean: git status --porcelain
            _completed_process(stdout=" M app.js\n"),
            # git stash push
            _completed_process(stdout="Saved working directory"),
            # git merge
            _completed_process(stdout="Merge made"),
            # git stash pop
            _completed_process(stdout="Applied stash"),
        ]
        ok, err = merge_branch("agent/auto-abc", "auto-abc", "add feature")
        assert ok is True
        assert err == ""
        # Verify stash was called with the bead ID in the message
        stash_call = mock_run.call_args_list[1]
        assert "dispatcher-auto-stash-auto-abc" in stash_call[0][0][3]

    @patch("agents.dispatcher.subprocess.run")
    def test_dirty_tree_stash_fails(self, mock_run):
        """Dirty tree + stash fails = (False, actionable error with file list)."""
        mock_run.side_effect = [
            # check_working_tree_clean: git status --porcelain
            _completed_process(stdout=" M app.js\n M base.html\n"),
            # git stash push (fails)
            _completed_process(returncode=1, stderr="error: cannot stash"),
        ]
        ok, err = merge_branch("agent/auto-def", "auto-def", "cleanup")
        assert ok is False
        assert "Dirty working tree blocks merge" in err
        assert "stash failed" in err
        assert "app.js" in err

    @patch("agents.dispatcher.subprocess.run")
    def test_dirty_tree_stash_ok_merge_fails_restore(self, mock_run):
        """Dirty tree + stash ok + merge fails + stash pop restores."""
        mock_run.side_effect = [
            # check_working_tree_clean: git status --porcelain
            _completed_process(stdout=" M config.py\n"),
            # git stash push
            _completed_process(stdout="Saved working directory"),
            # git merge (fails with conflict)
            _completed_process(returncode=1, stderr="CONFLICT in routes.py"),
            # git merge --abort
            _completed_process(),
            # git stash pop
            _completed_process(stdout="Applied stash"),
        ]
        ok, err = merge_branch("agent/auto-ghi", "auto-ghi", "refactor")
        assert ok is False
        assert "Merge conflict" in err
        # Verify stash pop was called to restore local changes
        pop_call = mock_run.call_args_list[4]
        assert pop_call[0][0] == ["git", "stash", "pop"]

    @patch("agents.dispatcher.subprocess.run")
    def test_dirty_tree_stash_ok_merge_ok_pop_fails(self, mock_run):
        """Dirty tree + stash + merge ok + stash pop fails = merge still succeeds."""
        mock_run.side_effect = [
            # check_working_tree_clean: git status --porcelain
            _completed_process(stdout=" M app.js\n"),
            # git stash push
            _completed_process(stdout="Saved working directory"),
            # git merge (succeeds)
            _completed_process(stdout="Merge made"),
            # git stash pop (fails — stash preserved for manual recovery)
            _completed_process(returncode=1, stderr="CONFLICT in app.js"),
        ]
        ok, err = merge_branch("agent/auto-jkl", "auto-jkl", "fix")
        # Merge succeeded even though pop failed — that's correct behavior
        assert ok is True
        assert err == ""
