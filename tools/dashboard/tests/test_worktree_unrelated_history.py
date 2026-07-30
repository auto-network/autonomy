"""Worktree sweep must stay cheap when a branch shares no history with base.

A base-branch history rewrite (the public-repo cleanup on 2026-07-30) left
~70 session branches with no common ancestor with master. ``git cherry`` /
``rev-list base..HEAD`` / ``diff base...HEAD`` then scan the branch's whole
history, and the startup worktree sweep hung for 55s. These tests build a
branch with an unrelated history and confirm the pending/ahead/empty
computations short-circuit to "nothing", while a normal descendant branch
still reports its real pending commits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import workspace_manager as wm


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    _init(r)
    _commit(r, "base.txt", "base\n")
    return r


def test_related_branch_reports_pending(repo):
    """A normal branch that descends from master reports its ahead commit."""
    _git(repo, "checkout", "-q", "-b", "feature")
    sha = _commit(repo, "feature.txt", "work\n")

    assert wm._shares_history_with_base(repo, "master") is True
    assert wm._worktree_commits_ahead(repo, base_ref="master") == 1
    pending = wm._dashboard_pending_commit_shas(repo, "autonomy", base_ref="master")
    assert pending == [sha]


def test_unrelated_history_branch_short_circuits(repo):
    """A branch with no common ancestor with master (an orphan, as a base
    rewrite produces) reports nothing pending without scanning its history."""
    # An orphan branch shares no commit with master — exactly the shape a
    # history rewrite leaves behind.
    _git(repo, "checkout", "-q", "--orphan", "rewritten")
    _git(repo, "rm", "-rfq", ".")
    _commit(repo, "other.txt", "unrelated\n")
    _commit(repo, "other2.txt", "more\n")

    assert wm._shares_history_with_base(repo, "master") is False
    # Every expensive computation short-circuits to "nothing pending".
    assert wm._worktree_commits_ahead(repo, base_ref="master") == 0
    assert wm._dashboard_pending_commit_shas(repo, "autonomy", base_ref="master") == []
    assert wm._worktree_net_empty(repo, base_ref="master") is False


def test_guard_does_not_run_cherry_on_unrelated_history(repo, monkeypatch):
    """The guard must fire BEFORE the O(all-commits) git commands, not after."""
    _git(repo, "checkout", "-q", "--orphan", "rewritten")
    _git(repo, "rm", "-rfq", ".")
    _commit(repo, "other.txt", "unrelated\n")

    calls = []
    real = wm._git_output

    def recording(args, cwd, *, timeout=15):
        calls.append(args[0] if args else "")
        return real(args, cwd, timeout=timeout)

    monkeypatch.setattr(wm, "_git_output", recording)
    wm._worktree_commits_ahead(repo, base_ref="master")
    wm._dashboard_pending_commit_shas(repo, "autonomy", base_ref="master")

    assert "cherry" not in calls
    assert "rev-list" not in calls
    assert calls.count("merge-base") >= 2  # the guard ran on each path
