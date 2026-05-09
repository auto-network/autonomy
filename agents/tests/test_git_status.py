from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from agents.git_status import (
    git_status_porcelain_lines,
    has_working_tree_changes,
    summarize_git_status_lines,
    working_tree_clean_and_summary,
)


def _completed_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestGitStatusPorcelainLines:
    @patch("agents.git_status.subprocess.run")
    def test_default_untracked_mode_uses_plain_porcelain(self, mock_run):
        mock_run.return_value = _completed_process(stdout="")
        rc, lines = git_status_porcelain_lines(Path("/tmp/repo"))
        assert rc == 0
        assert lines == []
        mock_run.assert_called_once_with(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd="/tmp/repo",
        )

    @patch("agents.git_status.subprocess.run")
    def test_tracked_only_mode_sets_untracked_no(self, mock_run):
        mock_run.return_value = _completed_process(stdout=" M tracked.py\n")
        rc, lines = git_status_porcelain_lines(Path("/tmp/repo"), untracked="no", timeout=9)
        assert rc == 0
        assert lines == [" M tracked.py"]
        mock_run.assert_called_once_with(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=9,
            cwd="/tmp/repo",
        )


def test_summarize_git_status_lines_truncates_after_ten():
    lines = [f" M file{i}.py" for i in range(15)]
    assert summarize_git_status_lines(lines) == (
        "; ".join(lines[:10]) + " ... and 5 more"
    )


class TestWorkingTreeCleanAndSummary:
    @patch("agents.git_status.subprocess.run")
    def test_clean_tree_returns_true_and_empty_summary(self, mock_run):
        mock_run.return_value = _completed_process(stdout="")
        assert working_tree_clean_and_summary(Path("/tmp/repo")) == (True, "")

    @patch("agents.git_status.subprocess.run")
    def test_dirty_tree_returns_summary(self, mock_run):
        mock_run.return_value = _completed_process(stdout="?? new_file.txt\n")
        assert working_tree_clean_and_summary(Path("/tmp/repo")) == (False, "?? new_file.txt")


class TestHasWorkingTreeChanges:
    @patch("agents.git_status.subprocess.run")
    def test_nonzero_status_treated_as_dirty_by_default(self, mock_run):
        mock_run.return_value = _completed_process(returncode=1, stderr="fatal")
        assert has_working_tree_changes(Path("/tmp/repo")) is True

    @patch("agents.git_status.subprocess.run")
    def test_nonzero_status_can_be_treated_as_clean(self, mock_run):
        mock_run.return_value = _completed_process(returncode=1, stderr="fatal")
        assert has_working_tree_changes(Path("/tmp/repo"), error_is_dirty=False) is False
