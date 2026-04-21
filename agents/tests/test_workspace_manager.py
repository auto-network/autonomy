"""Tests for agents.workspace_manager — clones, worktrees, mount specs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import workspace_manager as wm
from agents.workspace_settings import WorkspaceV1 as ProjectConfig, RepoMount


# ── URL parsing ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url, expected_host, expected_path",
    [
        ("git@github.com:anchore/enterprise.git", "github.com", "anchore/enterprise"),
        ("git@github.com:anchore/enterprise", "github.com", "anchore/enterprise"),
        ("https://github.com/foo/bar.git", "github.com", "foo/bar"),
        ("https://example.com/deep/path/repo.git", "example.com", "deep/path/repo"),
        ("ssh://git@gitlab.example.com:22/group/project.git", "gitlab.example.com", "group/project"),
    ],
)
def test_parse_repo_url(url, expected_host, expected_path):
    host, path = wm.parse_repo_url(url)
    assert host == expected_host
    assert path == expected_path


def test_parse_repo_url_rejects_garbage():
    with pytest.raises(wm.WorkspaceError, match="unrecognized git URL"):
        wm.parse_repo_url("not a url at all")


def test_managed_clone_path_layout(tmp_path):
    p = wm.managed_clone_path(
        "git@github.com:anchore/enterprise.git",
        repos_dir=tmp_path,
    )
    assert p == tmp_path / "github.com" / "anchore" / "enterprise.git"


# ── Clone + worktree round-trip against a local bare repo ──────────

def _make_upstream(tmp_path: Path) -> Path:
    """Build a bare repo with one commit so ``clone`` and ``worktree add`` work."""
    src = tmp_path / "upstream-src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(src), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "init"], check=True)

    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def test_ensure_managed_clone_clones_then_fetches(tmp_path, monkeypatch):
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"

    # Rewrite URL → on-disk clone location mapping for test isolation.
    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )

    first = wm.ensure_managed_clone(url, repos_dir=repos_dir)
    assert first.exists()
    assert (first / ".git").exists()

    # Second call should fetch, not re-clone. Easy signal: no error + same path.
    second = wm.ensure_managed_clone(url, repos_dir=repos_dir)
    assert second == first


def test_prepare_session_mounts_writable_and_readonly(tmp_path, monkeypatch):
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"
    worktrees_dir = tmp_path / "worktrees"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )
    # _worktree_basename uses parse_repo_url which only handles real URLs;
    # stub it to the upstream name.
    monkeypatch.setattr(wm, "_worktree_basename", lambda u: "upstream")

    proj_w = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    mounts_w = wm.prepare_session_mounts(
        proj_w, "sess-w", repos_dir=repos_dir, worktrees_dir=worktrees_dir,
    )
    worktree = worktrees_dir / "sess-w" / "upstream"
    clone = repos_dir / "local" / "upstream.git"
    assert mounts_w[str(worktree)] == "/workspace/upstream"
    # Clone must be mounted rw (no ``:ro`` suffix) so ``git add``/``commit``
    # in the worktree can write to ``<clone>/.git/worktrees/<name>/``.
    assert mounts_w[str(clone)] == str(clone)
    assert ":ro" not in mounts_w[str(clone)]
    assert worktree.exists()
    # Worktree should be on a fresh session branch.
    branch = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "session/sess-w"

    proj_ro = ProjectConfig(
        id="ro", name="ro", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/ro", writable=False),),
    )
    mounts_ro = wm.prepare_session_mounts(
        proj_ro, "sess-ro", repos_dir=repos_dir, worktrees_dir=worktrees_dir,
    )
    # Read-only path: only the clone itself is mounted, at the container path, ro.
    assert mounts_ro[str(clone)] == "/workspace/ro:ro"
    assert len(mounts_ro) == 1


def test_git_add_and_commit_succeed_in_session_worktree(tmp_path, monkeypatch):
    """Regression: ``git add``/``commit`` must succeed in a session worktree.

    The worktree's ``.git`` file points inside the managed clone, so every
    ``git`` write (index, refs, objects) hits the clone directory. If the
    clone is ever made read-only — as it once was via a ``:ro`` mount spec —
    ``git add`` fails with EROFS on ``.git/worktrees/<name>/index.lock``.
    This test exercises the full flow end-to-end and asserts the session
    branch advances past ``origin/HEAD``.
    """
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"
    worktrees_dir = tmp_path / "worktrees"
    session = "sess-commit"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )
    monkeypatch.setattr(wm, "_worktree_basename", lambda u: "upstream")

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj, session, repos_dir=repos_dir, worktrees_dir=worktrees_dir,
    )
    worktree = worktrees_dir / session / "upstream"
    clone = repos_dir / "local" / "upstream.git"
    branch = f"session/{session}"

    # Record origin/HEAD before the commit so we can prove HEAD advanced.
    head_before = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Configure identity and run the exact sequence that failed in NG sessions.
    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "hello.txt").write_text("hello\n")
    add = subprocess.run(
        ["git", "-C", str(worktree), "add", "hello.txt"],
        capture_output=True, text=True,
    )
    assert add.returncode == 0, (
        f"git add failed in session worktree: {add.stderr!r}. "
        "Check that the managed clone mount is not read-only."
    )
    commit = subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-m", "smoke"],
        capture_output=True, text=True,
    )
    assert commit.returncode == 0, f"git commit failed: {commit.stderr!r}"

    head_after = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after != head_before, "HEAD should advance after commit"

    # The session branch ref in the managed clone must point at the new HEAD.
    clone_branch_sha = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", branch],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert clone_branch_sha == head_after


def test_prepare_session_mounts_empty_for_repoless_project(tmp_path):
    proj = ProjectConfig(
        id="autonomy", name="autonomy", description="",
        image="img", graph_project="autonomy",
    )
    mounts = wm.prepare_session_mounts(
        proj, "sess", repos_dir=tmp_path / "r", worktrees_dir=tmp_path / "w",
    )
    assert mounts == {}


# ── Session cleanup ───────────────────────────────────────────────


def _make_writable_session_worktree(tmp_path: Path, session: str, monkeypatch):
    """Build an upstream + managed clone + session worktree for cleanup tests."""
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"
    worktrees_dir = tmp_path / "worktrees"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )
    monkeypatch.setattr(wm, "_worktree_basename", lambda u: "upstream")

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj, session, repos_dir=repos_dir, worktrees_dir=worktrees_dir,
    )
    clone = repos_dir / "local" / "upstream.git"
    worktree = worktrees_dir / session / "upstream"
    return worktrees_dir, clone, worktree


def test_cleanup_session_worktrees_removes_clean_worktree(tmp_path, monkeypatch):
    session = "sess-clean"
    worktrees_dir, clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    assert worktree.exists()

    # Branch exists on the managed clone.
    branches_before = subprocess.run(
        ["git", "-C", str(clone), "branch", "--list", f"session/{session}"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert f"session/{session}" in branches_before

    result = wm.cleanup_session_worktrees(session, worktrees_dir=worktrees_dir)
    assert str(worktree) in result.removed
    assert not result.preserved
    assert not result.errors

    assert not worktree.exists()
    assert not (worktrees_dir / session).exists(), "empty session dir should be removed"
    branches_after = subprocess.run(
        ["git", "-C", str(clone), "branch", "--list", f"session/{session}"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert branches_after.strip() == "", "session branch should be deleted"


def test_cleanup_preserves_worktree_with_uncommitted_changes(tmp_path, monkeypatch):
    session = "sess-dirty"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    # Leave an uncommitted modification.
    (worktree / "README.md").write_text("edited but not committed\n")

    result = wm.cleanup_session_worktrees(session, worktrees_dir=worktrees_dir)
    assert not result.removed
    assert len(result.preserved) == 1
    path, reason = result.preserved[0]
    assert path == str(worktree)
    assert "uncommitted" in reason
    assert worktree.exists()


def test_cleanup_preserves_worktree_with_unpushed_commits(tmp_path, monkeypatch):
    session = "sess-unpushed"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    # Commit a new change — ahead of origin/HEAD.
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "t"], check=True,
    )
    (worktree / "new.txt").write_text("new file\n")
    subprocess.run(["git", "-C", str(worktree), "add", "new.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-m", "ahead"], check=True,
    )

    result = wm.cleanup_session_worktrees(session, worktrees_dir=worktrees_dir)
    assert not result.removed
    assert len(result.preserved) == 1
    _path, reason = result.preserved[0]
    assert "unpushed" in reason


def test_cleanup_force_removes_dirty_worktree(tmp_path, monkeypatch):
    session = "sess-force"
    worktrees_dir, clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    (worktree / "README.md").write_text("edited\n")

    result = wm.cleanup_session_worktrees(
        session, force=True, worktrees_dir=worktrees_dir,
    )
    assert str(worktree) in result.removed
    assert not result.preserved
    assert not worktree.exists()
    # Session branch should still be deleted with force.
    branches = subprocess.run(
        ["git", "-C", str(clone), "branch", "--list", f"session/{session}"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert branches.strip() == ""


def test_cleanup_missing_session_is_noop(tmp_path):
    result = wm.cleanup_session_worktrees(
        "no-such-session", worktrees_dir=tmp_path / "worktrees",
    )
    assert not result.removed
    assert not result.preserved
    assert not result.errors


def test_prune_orphan_worktrees_skips_live_sessions(tmp_path, monkeypatch):
    # Two sessions: one "live", one "dead". Only the dead one should be cleaned.
    # We need both worktrees to target the SAME managed clone so the monkeypatched
    # managed_clone_path still works.
    session_live = "sess-live"
    session_dead = "sess-dead"
    worktrees_dir, _clone, wt_live = _make_writable_session_worktree(
        tmp_path, session_live, monkeypatch,
    )
    # Reuse the same upstream/clone for the second session.
    # _make_writable_session_worktree already set the monkeypatched path.
    url = str(next((tmp_path).glob("upstream.git")))
    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj, session_dead,
        repos_dir=tmp_path / "repos", worktrees_dir=worktrees_dir,
    )
    wt_dead = worktrees_dir / session_dead / "upstream"
    assert wt_dead.exists()
    assert wt_live.exists()

    results = wm.prune_orphan_worktrees(
        [session_live], worktrees_dir=worktrees_dir,
    )
    assert session_live not in results, "live sessions must be skipped"
    assert session_dead in results
    assert str(wt_dead) in results[session_dead].removed
    assert wt_live.exists()
    assert not wt_dead.exists()


def test_prune_orphan_worktrees_empty_dir(tmp_path):
    results = wm.prune_orphan_worktrees(
        [], worktrees_dir=tmp_path / "missing",
    )
    assert results == {}
