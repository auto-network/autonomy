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
    @patch("agents.dispatcher.working_tree_clean_and_summary")
    def test_clean_tree(self, mock_status):
        mock_status.return_value = (True, "")
        is_clean, summary = check_working_tree_clean()
        assert is_clean is True
        assert summary == ""
        mock_status.assert_called_once_with(REPO_ROOT, untracked="normal", timeout=10)

    @patch("agents.dispatcher.working_tree_clean_and_summary")
    def test_dirty_tree_single_file(self, mock_status):
        mock_status.return_value = (False, " M app.js")
        is_clean, summary = check_working_tree_clean()
        assert is_clean is False
        assert "app.js" in summary

    @patch("agents.dispatcher.working_tree_clean_and_summary")
    def test_dirty_tree_multiple_files(self, mock_status):
        mock_status.return_value = (False, "; ".join([f" M file{i}.py" for i in range(10)]) + " ... and 5 more")
        is_clean, summary = check_working_tree_clean()
        assert is_clean is False
        assert "and 5 more" in summary

    @patch("agents.dispatcher.working_tree_clean_and_summary")
    def test_untracked_files(self, mock_status):
        mock_status.return_value = (False, "?? new_file.txt")
        is_clean, summary = check_working_tree_clean()
        assert is_clean is False
        assert "new_file.txt" in summary


# ── merge_branch ────────────────────────────────────────────────


class TestMergeBranch:
    @patch("agents.dispatcher.check_working_tree_clean")
    @patch("agents.dispatcher.subprocess.run")
    def test_clean_tree_successful_merge(self, mock_run, mock_check):
        """Clean tree + successful merge = (True, "")."""
        mock_check.return_value = (True, "")
        mock_run.side_effect = [
            # git merge
            _completed_process(stdout="Merge made"),
        ]
        ok, err = merge_branch("agent/auto-xyz", "auto-xyz", "fix bug")
        assert ok is True
        assert err == ""

    @patch("agents.dispatcher.check_working_tree_clean")
    @patch("agents.dispatcher.subprocess.run")
    def test_clean_tree_merge_conflict(self, mock_run, mock_check):
        """Clean tree + merge conflict = (False, "Merge conflict: ...")."""
        mock_check.return_value = (True, "")
        mock_run.side_effect = [
            # git merge (fails)
            _completed_process(returncode=1, stderr="CONFLICT (content): Merge conflict in foo.py"),
            # git merge --abort
            _completed_process(),
        ]
        ok, err = merge_branch("agent/auto-xyz", "auto-xyz", "fix bug")
        assert ok is False
        assert "Merge conflict" in err
        assert "foo.py" in err

    @patch("agents.dispatcher.check_working_tree_clean")
    @patch("agents.dispatcher.subprocess.run")
    def test_dirty_tree_stash_merge_restore(self, mock_run, mock_check):
        """Dirty tree + stash succeeds + merge succeeds + stash pop succeeds."""
        mock_check.return_value = (False, " M app.js")
        mock_run.side_effect = [
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
        stash_call = mock_run.call_args_list[0]
        assert "dispatcher-auto-stash-auto-abc" in stash_call[0][0][4]

    @patch("agents.dispatcher.check_working_tree_clean")
    @patch("agents.dispatcher.subprocess.run")
    def test_dirty_tree_stash_fails(self, mock_run, mock_check):
        """Dirty tree + stash fails = (False, actionable error with file list)."""
        mock_check.return_value = (False, " M app.js;  M base.html")
        mock_run.side_effect = [
            # git stash push (fails)
            _completed_process(returncode=1, stderr="error: cannot stash"),
        ]
        ok, err = merge_branch("agent/auto-def", "auto-def", "cleanup")
        assert ok is False
        assert "Dirty working tree blocks merge" in err
        assert "stash failed" in err
        assert "app.js" in err

    @patch("agents.dispatcher.check_working_tree_clean")
    @patch("agents.dispatcher.subprocess.run")
    def test_dirty_tree_stash_ok_merge_fails_restore(self, mock_run, mock_check):
        """Dirty tree + stash ok + merge fails + stash pop restores."""
        mock_check.return_value = (False, " M config.py")
        mock_run.side_effect = [
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
        pop_call = mock_run.call_args_list[3]
        assert pop_call[0][0] == ["git", "stash", "pop"]

    @patch("agents.dispatcher.check_working_tree_clean")
    @patch("agents.dispatcher.subprocess.run")
    def test_dirty_tree_stash_ok_merge_ok_pop_fails_reverts_merge(self, mock_run, mock_check):
        """Stash pop fails after successful merge → revert merge, restore stash, return failure."""
        mock_check.return_value = (False, " M app.js")
        mock_run.side_effect = [
            # git stash push
            _completed_process(stdout="Saved working directory"),
            # git merge (succeeds)
            _completed_process(stdout="Merge made"),
            # git stash pop (fails — conflict with merged code)
            _completed_process(returncode=1, stderr="CONFLICT in app.js"),
            # git reset --hard HEAD~1 (revert merge)
            _completed_process(),
            # git stash pop (succeeds on reverted tree)
            _completed_process(stdout="Applied stash"),
        ]
        ok, err = merge_branch("agent/auto-jkl", "auto-jkl", "fix")
        assert ok is False
        assert err.startswith("STASH_POP_CONFLICT:")
        assert "auto-jkl" in err
        # Verify merge was reverted
        reset_call = mock_run.call_args_list[3]
        assert reset_call[0][0] == ["git", "reset", "--hard", "HEAD~1"]
        # Verify stash was re-popped
        pop2_call = mock_run.call_args_list[4]
        assert pop2_call[0][0] == ["git", "stash", "pop"]

    @patch("agents.dispatcher.check_working_tree_clean")
    @patch("agents.dispatcher.subprocess.run")
    def test_stash_pop_fail_stash_restored_after_revert(self, mock_run, mock_check):
        """After reverting merge, stashed changes are restored (stash list empty)."""
        mock_check.return_value = (False, " M session_monitor.py")
        mock_run.side_effect = [
            _completed_process(stdout="Saved working directory"),
            _completed_process(stdout="Merge made"),
            _completed_process(returncode=1, stderr="CONFLICT in session_monitor.py"),
            _completed_process(),  # git reset --hard HEAD~1
            _completed_process(stdout="Applied stash"),  # second pop succeeds
        ]
        ok, err = merge_branch("agent/auto-d0w6", "auto-d0w6", "refactor")
        assert ok is False
        assert "STASH_POP_CONFLICT" in err
        # Second stash pop was called and succeeded → stash is clean
        assert mock_run.call_args_list[4][0][0] == ["git", "stash", "pop"]

    @patch("agents.dispatcher.check_working_tree_clean")
    @patch("agents.dispatcher.subprocess.run")
    def test_stash_pop_succeeds_unchanged(self, mock_run, mock_check):
        """Stash pop succeeds → existing behavior unchanged, returns (True, "")."""
        mock_check.return_value = (False, " M app.js")
        mock_run.side_effect = [
            _completed_process(stdout="Saved working directory"),
            _completed_process(stdout="Merge made"),
            _completed_process(stdout="Applied stash"),
        ]
        ok, err = merge_branch("agent/auto-ok1", "auto-ok1", "feature")
        assert ok is True
        assert err == ""

    @patch("agents.dispatcher.check_working_tree_clean")
    @patch("agents.dispatcher.subprocess.run")
    def test_stash_pop_fail_no_conflict_markers(self, mock_run, mock_check):
        """After stash pop failure + revert, working tree is clean (no conflict markers)."""
        mock_check.return_value = (False, " M routes.py")
        mock_run.side_effect = [
            _completed_process(stdout="Saved working directory"),
            _completed_process(stdout="Merge made"),
            _completed_process(returncode=1, stderr="CONFLICT in routes.py"),
            _completed_process(),  # git reset --hard HEAD~1 succeeds
            _completed_process(stdout="Applied stash"),  # stash restored cleanly
        ]
        ok, err = merge_branch("agent/auto-mrk", "auto-mrk", "update")
        assert ok is False
        # Merge was reverted (reset --hard) → no conflict markers possible
        reset_call = mock_run.call_args_list[3]
        assert reset_call[0][0] == ["git", "reset", "--hard", "HEAD~1"]
        # Stash was restored cleanly
        pop2_call = mock_run.call_args_list[4]
        assert pop2_call[0][0] == ["git", "stash", "pop"]

    @patch("agents.dispatcher.check_working_tree_clean")
    @patch("agents.dispatcher.subprocess.run")
    def test_stash_pop_fail_second_pop_also_fails(self, mock_run, mock_check):
        """If second stash pop also fails, still returns STASH_POP_CONFLICT."""
        mock_check.return_value = (False, " M app.js")
        mock_run.side_effect = [
            _completed_process(stdout="Saved working directory"),
            _completed_process(stdout="Merge made"),
            _completed_process(returncode=1, stderr="CONFLICT in app.js"),
            _completed_process(),  # git reset --hard HEAD~1
            _completed_process(returncode=1, stderr="cannot pop stash"),  # second pop fails too
        ]
        ok, err = merge_branch("agent/auto-x2f", "auto-x2f", "fix")
        assert ok is False
        assert err.startswith("STASH_POP_CONFLICT:")
