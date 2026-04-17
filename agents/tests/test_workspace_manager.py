"""Tests for agents.workspace_manager — clones, worktrees, mount specs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import workspace_manager as wm
from agents.project_config import ProjectConfig, RepoMount


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
    assert mounts_w[str(clone)] == f"{clone}:ro"
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


def test_prepare_session_mounts_empty_for_repoless_project(tmp_path):
    proj = ProjectConfig(
        id="autonomy", name="autonomy", description="",
        image="img", graph_project="autonomy",
    )
    mounts = wm.prepare_session_mounts(
        proj, "sess", repos_dir=tmp_path / "r", worktrees_dir=tmp_path / "w",
    )
    assert mounts == {}
