from __future__ import annotations

from pathlib import Path

from agents import workspace_manager as wm
from tools.dashboard.dao import commit_workflow_db


def _commit(sha: str) -> wm.WorktreeCommit:
    return wm.WorktreeCommit(
        sha=sha,
        short_sha=sha[:7],
        subject=f"commit {sha}",
        author="Agent",
        date="2026-06-09",
        body="",
    )


def test_scan_filters_displayed_commits_but_keeps_git_topology_flags(tmp_path, monkeypatch):
    db_path = tmp_path / "commit_workflow.db"
    monkeypatch.setattr(commit_workflow_db, "DB_PATH", db_path)
    commit_workflow_db.append_event(
        event_id="e-landed-a",
        workflow_id="wf-a",
        event_type="landed",
        status_after="landed",
        repo_slug="autonomy",
        commit_shas=["A" * 40],
        db_path=db_path,
    )

    worktree = tmp_path / "worktrees" / "auto-x" / "autonomy"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /elsewhere")
    clone = tmp_path / "repos" / "autonomy.git"
    clone.mkdir(parents=True)

    raw_commits = [_commit("A" * 40), _commit("B" * 40)]
    rebase_calls: list[bool] = []
    cherry_commit_counts: list[int] = []

    monkeypatch.setattr(wm, "_find_managed_clone_for_worktree", lambda _path: clone)
    monkeypatch.setattr(wm, "_worktree_branch_name", lambda _path: "session/auto-x")
    monkeypatch.setattr(wm, "_worktree_dashboard_base_ref", lambda *_args, **_kwargs: "base")
    monkeypatch.setattr(wm, "_worktree_clone_stale", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(wm, "_worktree_dirty_files", lambda _path: [])
    monkeypatch.setattr(wm, "_worktree_commits", lambda *_args, **_kwargs: list(raw_commits))
    monkeypatch.setattr(wm, "_worktree_commits_ahead", lambda *_args, **_kwargs: len(raw_commits))
    monkeypatch.setattr(wm, "_worktree_ff_only_safe", lambda *_args, **_kwargs: True)

    def fake_rebase_required(
        _path: Path,
        _repo: str,
        *,
        has_pending_commits: bool,
        clone_stale: bool,
        target_branch_and_head: tuple[str | None, str | None] | None = None,
    ) -> bool:
        rebase_calls.append(has_pending_commits)
        return has_pending_commits and not clone_stale

    monkeypatch.setattr(wm, "_worktree_rebase_required", fake_rebase_required)
    def fake_cherry_pick_eligibility(**kwargs):
        cherry_commit_counts.append(len(kwargs["commits"]))
        return False, None

    monkeypatch.setattr(wm, "_compute_cherry_pick_eligibility", fake_cherry_pick_eligibility)

    rows = wm.scan_all_worktrees(worktrees_dir=tmp_path / "worktrees", live_session_names={"auto-x"})

    assert len(rows) == 1
    row = rows[0]
    assert [commit.sha for commit in row.commits] == ["B" * 40]
    assert row.commits_ahead == 1
    assert rebase_calls == [True]
    assert cherry_commit_counts == [2]
    assert row.rebase_required is True
    assert row.ff_eligible is False
    assert row.cherry_pick_eligible is False
    assert row.cherry_pick_commit is None


def test_scan_reverted_back_on_target_is_absent_when_git_prefilter_removes_it(tmp_path, monkeypatch):
    db_path = tmp_path / "commit_workflow.db"
    monkeypatch.setattr(commit_workflow_db, "DB_PATH", db_path)
    commit_workflow_db.append_event(
        event_id="e-proposed-a",
        workflow_id="wf-a",
        event_type="proposed",
        status_after="proposed",
        repo_slug="autonomy",
        commit_shas=["A" * 40],
        db_path=db_path,
    )
    commit_workflow_db.append_event(
        event_id="e-reverted-a",
        workflow_id="wf-a",
        event_type="reverted",
        status_after="reverted",
        repo_slug="autonomy",
        commit_shas=[],
        db_path=db_path,
    )

    worktree = tmp_path / "worktrees" / "auto-x" / "autonomy"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /elsewhere")
    clone = tmp_path / "repos" / "autonomy.git"
    clone.mkdir(parents=True)

    # _worktree_commits has already applied _dashboard_pending_commit_shas.
    # Simulate git saying reverted A is back on target by not returning it.
    raw_commits = [_commit("B" * 40)]

    monkeypatch.setattr(wm, "_find_managed_clone_for_worktree", lambda _path: clone)
    monkeypatch.setattr(wm, "_worktree_branch_name", lambda _path: "session/auto-x")
    monkeypatch.setattr(wm, "_worktree_dashboard_base_ref", lambda *_args, **_kwargs: "base")
    monkeypatch.setattr(wm, "_worktree_clone_stale", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(wm, "_worktree_dirty_files", lambda _path: [])
    monkeypatch.setattr(wm, "_worktree_commits", lambda *_args, **_kwargs: list(raw_commits))
    monkeypatch.setattr(wm, "_worktree_commits_ahead", lambda *_args, **_kwargs: len(raw_commits))
    monkeypatch.setattr(wm, "_worktree_rebase_required", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(wm, "_worktree_ff_only_safe", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(wm, "_compute_cherry_pick_eligibility", lambda **_kwargs: (False, None))

    rows = wm.scan_all_worktrees(worktrees_dir=tmp_path / "worktrees", live_session_names={"auto-x"})

    assert len(rows) == 1
    assert [commit.sha for commit in rows[0].commits] == ["B" * 40]
    assert rows[0].commits_ahead == 1
