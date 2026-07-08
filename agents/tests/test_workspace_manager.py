"""Tests for agents.workspace_manager — clones, worktrees, mount specs."""

from __future__ import annotations

import logging
import os
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


# ── Local-first repos (no git remote, e.g. a git-svn mirror) ───────

@pytest.mark.parametrize(
    "url, expected",
    [
        ("/home/jeremy/workspace/dynbench", True),
        ("/abs/path", True),
        ("git@github.com:anchore/enterprise.git", False),
        ("https://github.com/foo/bar.git", False),
        ("ssh://git@gitlab.example.com:22/group/project.git", False),
        ("admin@5.161.244.118:/opt/git/infra.git", False),
    ],
)
def test_is_local_url(url, expected):
    assert wm._is_local_url(url) is expected


def test_managed_clone_path_local(tmp_path):
    p = wm.managed_clone_path("/home/jeremy/workspace/dynbench", repos_dir=tmp_path)
    assert p == tmp_path / "local" / "home/jeremy/workspace/dynbench.git"


def test_worktree_basename_local():
    assert wm._worktree_basename("/home/jeremy/workspace/dynbench") == "dynbench"
    # parse_repo_url stays strict for non-URLs — only the local helpers accept paths.
    with pytest.raises(wm.WorkspaceError):
        wm.parse_repo_url("/home/jeremy/workspace/dynbench")


def _make_local_checkout(tmp_path: Path) -> Path:
    """A non-bare git checkout with a commit and NO remote (git-svn-mirror shape)."""
    src = tmp_path / "local-checkout"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=src, check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "Solution.sln").write_text("solution\n")
    subprocess.run(["git", "-C", str(src), "add", "Solution.sln"], check=True)
    subprocess.run([
        "git", "-C", str(src), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init",
    ], check=True)
    # Deliberately NO `git remote add origin` — this is the crux of the case.
    return src


def test_prepare_session_mounts_local_repo_no_remote(tmp_path):
    """A local checkout with no git remote provisions cleanly (option C).

    No monkeypatching of managed_clone_path/_worktree_basename: the local-path
    handling must work natively, and base_source reconciliation (which would
    fail on a remote-less checkout) must be skipped.
    """
    checkout = _make_local_checkout(tmp_path)
    url = str(checkout)
    repos_dir = tmp_path / "repos"
    worktrees_dir = tmp_path / "worktrees"

    proj = ProjectConfig(
        id="db", name="db", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, base_source=url, mount="/workspace/db", writable=True),),
    )
    mounts = wm.prepare_session_mounts(
        proj, "sess-db", repos_dir=repos_dir, worktrees_dir=worktrees_dir,
    )

    worktree = worktrees_dir / "sess-db" / "local-checkout"
    clone = repos_dir / "local" / f"{url.strip('/')}.git"
    # Managed clone is self-contained under repos_dir/local/… and mounted rw.
    # (Non-bare clone, so its git dir is under .git/.)
    assert clone.exists() and (clone / ".git").exists()
    assert mounts[str(worktree)] == "/workspace/db"
    assert mounts[str(clone)] == str(clone)
    # Real worktree (not a hollow shell), on a fresh session branch, tree present.
    assert (worktree / ".git").exists()
    assert (worktree / "Solution.sln").exists()
    branch = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "session/sess-db"


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
    subprocess.run([
        "git", "-C", str(src), "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "init",
    ], check=True)

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
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "smoke",
        ],
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


def test_create_worktree_prefers_explicitly_synced_local_base_when_ahead_of_origin(tmp_path, monkeypatch):
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )

    clone = wm.ensure_managed_clone(url, repos_dir=repos_dir)
    host = _make_host_checkout(tmp_path, upstream, "host-local-base")
    (host / "local-base.txt").write_text("local base\n")
    subprocess.run(["git", "-C", str(host), "add", "local-base.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(host), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "local base ahead",
        ],
        check=True,
    )
    wm._sync_managed_clone_branch_ref(clone, host, "main")
    local_head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    origin_head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "origin/HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert local_head != origin_head

    worktree = tmp_path / "worktrees" / "sess-local-base" / "upstream"
    wm.create_worktree(clone, worktree, "session/sess-local-base")

    worktree_head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert worktree_head == local_head
    assert (worktree / "local-base.txt").exists()


def test_prepare_session_mounts_refreshes_existing_clean_worktree_on_fresh_launch(tmp_path, monkeypatch):
    session = "sess-refresh"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    upstream = next(tmp_path.glob("upstream.git"))
    url = str(upstream)

    dev = tmp_path / "dev-refresh"
    subprocess.run(["git", "clone", "-q", str(upstream), str(dev)], check=True)
    subprocess.run(["git", "-C", str(dev), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(dev), "config", "user.name", "t"], check=True)
    (dev / "latest.txt").write_text("latest\n")
    subprocess.run(["git", "-C", str(dev), "add", "latest.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(dev), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "latest upstream",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(dev), "push", "-q", "origin", "main"], check=True)

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj,
        session,
        repos_dir=tmp_path / "repos",
        worktrees_dir=worktrees_dir,
        refresh_existing_worktree=True,
    )

    assert (worktree / "latest.txt").exists(), "fresh launch should advance reused clean worktree"


def test_prepare_session_mounts_refreshes_existing_clean_worktree_to_local_base_when_ahead_of_origin(tmp_path, monkeypatch):
    session = "sess-refresh-local-base"
    worktrees_dir, clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    upstream = next(tmp_path.glob("upstream.git"))
    url = str(upstream)

    host = _make_host_checkout(tmp_path, upstream, "host-refresh-local-base")
    (host / "local-base.txt").write_text("local base\n")
    subprocess.run(["git", "-C", str(host), "add", "local-base.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(host), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "local base ahead",
        ],
        check=True,
    )
    wm._sync_managed_clone_branch_ref(clone, host, "main")

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj,
        session,
        repos_dir=tmp_path / "repos",
        worktrees_dir=worktrees_dir,
        refresh_existing_worktree=True,
    )

    assert (worktree / "local-base.txt").exists(), "fresh launch should sync reused worktree to local base branch"


def test_prepare_session_mounts_refreshes_existing_clean_worktree_to_origin_when_clone_main_diverges(tmp_path, monkeypatch):
    session = "sess-refresh-diverged-clone"
    worktrees_dir, clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    upstream = next(tmp_path.glob("upstream.git"))
    url = str(upstream)

    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
    (clone / "local-base.txt").write_text("local base\n")
    subprocess.run(["git", "-C", str(clone), "add", "local-base.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(clone), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "local base ahead",
        ],
        check=True,
    )

    dev = tmp_path / "dev-refresh-diverged"
    subprocess.run(["git", "clone", "-q", str(upstream), str(dev)], check=True)
    subprocess.run(["git", "-C", str(dev), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(dev), "config", "user.name", "t"], check=True)
    (dev / "latest.txt").write_text("latest\n")
    subprocess.run(["git", "-C", str(dev), "add", "latest.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(dev), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "latest upstream",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(dev), "push", "-q", "origin", "main"], check=True)

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj,
        session,
        repos_dir=tmp_path / "repos",
        worktrees_dir=worktrees_dir,
        refresh_existing_worktree=True,
    )

    assert (worktree / "latest.txt").exists(), (
        "fresh launch should self-heal a divergent clone main back to origin"
    )
    assert not (worktree / "local-base.txt").exists(), (
        "unprovenanced clone-local commits must not pin future launches"
    )


def test_prepare_session_mounts_preserves_existing_worktree_with_local_commits(tmp_path, monkeypatch):
    session = "sess-preserve"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    upstream = next(tmp_path.glob("upstream.git"))
    url = str(upstream)

    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "mine.txt").write_text("keep me\n")
    subprocess.run(["git", "-C", str(worktree), "add", "mine.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "local work",
        ],
        check=True,
    )
    local_head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    dev = tmp_path / "dev-preserve"
    subprocess.run(["git", "clone", "-q", str(upstream), str(dev)], check=True)
    subprocess.run(["git", "-C", str(dev), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(dev), "config", "user.name", "t"], check=True)
    (dev / "latest.txt").write_text("latest\n")
    subprocess.run(["git", "-C", str(dev), "add", "latest.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(dev), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "latest upstream",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(dev), "push", "-q", "origin", "main"], check=True)

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj,
        session,
        repos_dir=tmp_path / "repos",
        worktrees_dir=worktrees_dir,
        refresh_existing_worktree=True,
    )

    head_after = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after == local_head, "refresh path must preserve existing local commits"
    assert (worktree / "mine.txt").exists()


def test_prepare_session_mounts_readonly_prefers_explicitly_synced_local_default_branch_when_ahead_of_origin(tmp_path, monkeypatch):
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )

    clone = wm.ensure_managed_clone(url, repos_dir=repos_dir)
    host = _make_host_checkout(tmp_path, upstream, "host-ro-local-base")
    (host / "readonly-local.txt").write_text("readonly local\n")
    subprocess.run(["git", "-C", str(host), "add", "readonly-local.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(host), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "readonly local base ahead",
        ],
        check=True,
    )
    wm._sync_managed_clone_branch_ref(clone, host, "main")
    local_head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    proj = ProjectConfig(
        id="ro", name="ro", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/ro", writable=False),),
    )
    mounts = wm.prepare_session_mounts(
        proj, "sess-ro-local", repos_dir=repos_dir, worktrees_dir=tmp_path / "worktrees",
    )

    clone_head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert clone_head == local_head
    assert mounts[str(clone)] == "/workspace/ro:ro"


# ── base_source host-checkout sync (auto-4sfe9) ───────────────────


def _make_host_checkout(tmp_path: Path, upstream: Path, name: str) -> Path:
    """Clone ``upstream`` into ``tmp_path/name`` and return the checkout path."""
    dest = tmp_path / name
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(dest)], check=True,
    )
    subprocess.run(["git", "-C", str(dest), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.name", "t"], check=True)
    return dest


def test_prepare_session_mounts_syncs_clone_main_from_base_source(tmp_path, monkeypatch):
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"
    worktrees_dir = tmp_path / "worktrees"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )
    monkeypatch.setattr(wm, "_worktree_basename", lambda u: "upstream")

    host = _make_host_checkout(tmp_path, upstream, "host-checkout")
    (host / "host-only.txt").write_text("host only\n")
    subprocess.run(["git", "-C", str(host), "add", "host-only.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(host), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "host-only commit",
        ],
        check=True,
    )
    host_head = subprocess.run(
        ["git", "-C", str(host), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(
            url=url,
            mount="/workspace/upstream",
            writable=True,
            base_source=str(host),
        ),),
    )
    wm.prepare_session_mounts(
        proj, "sess-base-src",
        repos_dir=repos_dir, worktrees_dir=worktrees_dir,
    )

    clone = repos_dir / "local" / "upstream.git"
    clone_main = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert clone_main == host_head, (
        "managed clone's main should track the base_source host checkout"
    )

    worktree = worktrees_dir / "sess-base-src" / "upstream"
    assert (worktree / "host-only.txt").exists(), (
        "fresh worktree should derive from the base_source-advanced main"
    )


def test_prepare_session_mounts_readonly_advances_clone_main_from_base_source(
    tmp_path, monkeypatch,
):
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )

    host = _make_host_checkout(tmp_path, upstream, "host-checkout-ro")
    (host / "ro-host-only.txt").write_text("ro host only\n")
    subprocess.run(["git", "-C", str(host), "add", "ro-host-only.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(host), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "ro host commit",
        ],
        check=True,
    )
    host_head = subprocess.run(
        ["git", "-C", str(host), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    proj = ProjectConfig(
        id="ro", name="ro", description="", image="img", graph_project="gp",
        repos=(RepoMount(
            url=url,
            mount="/workspace/ro",
            writable=False,
            base_source=str(host),
        ),),
    )
    wm.prepare_session_mounts(
        proj, "sess-ro-base-src",
        repos_dir=repos_dir, worktrees_dir=tmp_path / "worktrees",
    )

    clone = repos_dir / "local" / "upstream.git"
    clone_main = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert clone_main == host_head, (
        "read-only managed clone's main ref should track the base_source host checkout"
    )


def test_prepare_session_mounts_rejects_missing_base_source_path(tmp_path, monkeypatch):
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(
            url=url,
            mount="/workspace/upstream",
            writable=True,
            base_source="/nonexistent/path/that/should/not/exist",
        ),),
    )
    with pytest.raises(wm.WorkspaceError, match="base_source"):
        wm.prepare_session_mounts(
            proj, "sess-bad-path",
            repos_dir=repos_dir, worktrees_dir=tmp_path / "worktrees",
        )


def test_prepare_session_mounts_rejects_non_git_base_source(tmp_path, monkeypatch):
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )

    not_a_git_dir = tmp_path / "not-a-git-checkout"
    not_a_git_dir.mkdir()

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(
            url=url,
            mount="/workspace/upstream",
            writable=True,
            base_source=str(not_a_git_dir),
        ),),
    )
    with pytest.raises(wm.WorkspaceError, match="not a git checkout"):
        wm.prepare_session_mounts(
            proj, "sess-not-git",
            repos_dir=repos_dir, worktrees_dir=tmp_path / "worktrees",
        )


def test_prepare_session_mounts_rejects_base_source_identity_mismatch(tmp_path, monkeypatch):
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )

    # Build a second unrelated upstream so the host checkout's origin
    # parses fine but does not match the workspace repo URL.
    other_src = tmp_path / "other-src"
    other_src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other_src, check=True)
    subprocess.run(["git", "-C", str(other_src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(other_src), "config", "user.name", "t"], check=True)
    (other_src / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(other_src), "add", "README.md"], check=True)
    subprocess.run([
        "git", "-C", str(other_src), "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "init",
    ], check=True)
    other_bare = tmp_path / "other.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(other_src), str(other_bare)],
        check=True,
    )
    host = _make_host_checkout(tmp_path, other_bare, "host-mismatch")

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(
            url=url,
            mount="/workspace/upstream",
            writable=True,
            base_source=str(host),
        ),),
    )
    with pytest.raises(wm.WorkspaceError, match="does not match repo URL"):
        wm.prepare_session_mounts(
            proj, "sess-mismatch",
            repos_dir=repos_dir, worktrees_dir=tmp_path / "worktrees",
        )


def test_prepare_session_mounts_rejects_relative_base_source(tmp_path, monkeypatch):
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(
            url=url,
            mount="/workspace/upstream",
            writable=True,
            base_source="relative/path",
        ),),
    )
    with pytest.raises(wm.WorkspaceError, match="absolute path"):
        wm.prepare_session_mounts(
            proj, "sess-rel",
            repos_dir=repos_dir, worktrees_dir=tmp_path / "worktrees",
        )


def test_prepare_session_mounts_resume_preserves_session_worktree_with_base_source(
    tmp_path, monkeypatch,
):
    """Resume must not reset/rebase a session worktree even when base_source advances the clone."""
    session = "sess-resume-base-src"
    worktrees_dir, clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    upstream = next(tmp_path.glob("upstream.git"))
    url = str(upstream)

    # Land local session work on the worktree.
    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "session-work.txt").write_text("keep me\n")
    subprocess.run(["git", "-C", str(worktree), "add", "session-work.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "session work",
        ],
        check=True,
    )
    head_before = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Build a host checkout that has advanced beyond the bare upstream.
    host = _make_host_checkout(tmp_path, upstream, "host-resume")
    (host / "host-advance.txt").write_text("host advance\n")
    subprocess.run(["git", "-C", str(host), "add", "host-advance.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(host), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "host advance",
        ],
        check=True,
    )

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(
            url=url,
            mount="/workspace/upstream",
            writable=True,
            base_source=str(host),
        ),),
    )
    # Resume path: refresh_existing_worktree=False.
    wm.prepare_session_mounts(
        proj, session,
        repos_dir=tmp_path / "repos",
        worktrees_dir=worktrees_dir,
        refresh_existing_worktree=False,
    )

    head_after = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after == head_before, (
        "resume must preserve the session worktree's HEAD even when base_source moves the clone"
    )
    assert (worktree / "session-work.txt").exists()


# ── Session cleanup ───────────────────────────────────────────────


def _make_writable_session_worktree(
    tmp_path: Path,
    session: str,
    monkeypatch,
    *,
    repo_name: str = "upstream",
):
    """Build an upstream + managed clone + session worktree for cleanup tests."""
    upstream = _make_upstream(tmp_path)
    url = str(upstream)
    repos_dir = tmp_path / "repos"
    worktrees_dir = tmp_path / "worktrees"

    monkeypatch.setattr(
        wm, "managed_clone_path",
        lambda u, *, repos_dir=repos_dir: repos_dir / "local" / "upstream.git",
    )
    monkeypatch.setattr(wm, "_worktree_basename", lambda u: repo_name)

    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj, session, repos_dir=repos_dir, worktrees_dir=worktrees_dir,
    )
    clone = repos_dir / "local" / "upstream.git"
    worktree = worktrees_dir / session / repo_name
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


def test_cleanup_preserves_worktree_with_local_commits(tmp_path, monkeypatch):
    session = "sess-local-commits"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    # Commit a new change — ahead of the current integration base.
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "t"], check=True,
    )
    (worktree / "new.txt").write_text("new file\n")
    subprocess.run(["git", "-C", str(worktree), "add", "new.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "ahead",
        ],
        check=True,
    )

    result = wm.cleanup_session_worktrees(session, worktrees_dir=worktrees_dir)
    assert not result.removed
    assert len(result.preserved) == 1
    _path, reason = result.preserved[0]
    assert "local commits" == reason


def test_cleanup_removes_worktree_after_cherry_pick(tmp_path, monkeypatch):
    """Cherry-picked commits must not keep a worktree alive forever.

    Regression for the cleanup-vs-dashboard ahead-counter divergence:
    the dashboard's ``_worktree_commits_ahead`` does patch-id matching
    via ``git cherry``, but the cleanup preserve-check used to do raw
    ``rev-list --count base..HEAD``. After a session's commit got
    cherry-picked onto master with a new SHA, raw rev-list still saw
    "1 ahead" and the worktree was preserved indefinitely, even though
    the dashboard reported ``commits_ahead=0``. This test pins the
    fixed behaviour: same patch-id on base ⇒ worktree is removable.
    """
    session = "sess-cherry"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "t"], check=True,
    )

    # Commit a change on the session branch.
    (worktree / "feature.txt").write_text("feature work\n")
    subprocess.run(["git", "-C", str(worktree), "add", "feature.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "feature",
        ],
        check=True,
    )
    session_sha = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Synthesize a cherry-pick onto main: same tree, different parent ⇒
    # different SHA, identical patch-id. ``git cherry`` then sees the
    # session commit's patch as already present on main.
    main_parent = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    feature_tree = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", f"{session_sha}^{{tree}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    new_main_sha = subprocess.run(
        [
            "git", "-C", str(worktree),
            "-c", "commit.gpgsign=false",
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit-tree", feature_tree, "-p", main_parent,
            "-m", "cherry-pick of feature",
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(worktree), "update-ref",
         "refs/heads/main", new_main_sha],
        check=True,
    )
    # A real landing reaches origin too (push, or the host sync that also
    # stamps the synced-source marker). Keep the remote-tracking ref in
    # step: the preserve-check's base (``_repo_integration_base_ref``)
    # prefers ``origin/main`` when local and remote diverge without a
    # synced-source marker.
    subprocess.run(
        ["git", "-C", str(worktree), "update-ref",
         "refs/remotes/origin/main", new_main_sha],
        check=True,
    )

    # Sanity: SHAs differ, but ``git cherry`` reports zero "+ " lines.
    assert session_sha != new_main_sha
    cherry_out = subprocess.run(
        ["git", "-C", str(worktree), "cherry", "main", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    plus_lines = [ln for ln in cherry_out.splitlines() if ln.startswith("+ ")]
    assert plus_lines == [], (
        f"expected no '+ ' lines from `git cherry main HEAD`, got: {cherry_out!r}"
    )

    # The fixed preserve-check sees patch-id-on-base ⇒ remove.
    result = wm.cleanup_session_worktrees(
        session, worktrees_dir=worktrees_dir,
    )
    assert str(worktree) in result.removed, (
        f"expected worktree removed; "
        f"preserved={result.preserved} errors={result.errors}"
    )
    assert not result.preserved
    assert not worktree.exists()


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


def test_scan_all_worktrees_reports_state_per_session(tmp_path, monkeypatch):
    session_live = "sess-live"
    session_dirty = "sess-dirty"
    session_clean = "sess-clean"
    worktrees_dir, clone, wt_live = _make_writable_session_worktree(
        tmp_path, session_live, monkeypatch,
    )

    url = str(next(tmp_path.glob("upstream.git")))
    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj, session_dirty,
        repos_dir=tmp_path / "repos", worktrees_dir=worktrees_dir,
    )
    wm.prepare_session_mounts(
        proj, session_clean,
        repos_dir=tmp_path / "repos", worktrees_dir=worktrees_dir,
    )
    wt_dirty = worktrees_dir / session_dirty / "upstream"
    wt_clean = worktrees_dir / session_clean / "upstream"

    subprocess.run(["git", "-C", str(wt_live), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wt_live), "config", "user.name", "t"], check=True)
    (wt_live / "merged.txt").write_text("ready to merge\n")
    subprocess.run(["git", "-C", str(wt_live), "add", "merged.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(wt_live), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "ready",
        ],
        check=True,
    )

    (wt_dirty / "README.md").write_text("still editing\n")

    rows = wm.scan_all_worktrees(
        worktrees_dir=worktrees_dir,
        live_session_names={session_live},
    )
    by_session = {row.session_name: row for row in rows}

    live_row = by_session[session_live]
    assert live_row.repo_name == "upstream"
    assert live_row.worktree_path == wt_live
    assert live_row.managed_clone == clone
    assert live_row.branch == f"session/{session_live}"
    assert live_row.commits_ahead == 1
    assert live_row.is_dirty is False
    assert live_row.ff_eligible is True
    assert live_row.clone_stale is False
    assert live_row.session_live is True
    assert len(live_row.commits) == 1
    assert live_row.commits[0].subject == "ready"
    assert live_row.commits[0].files[0].path == "merged.txt"
    assert live_row.commits[0].files[0].additions == 1

    dirty_row = by_session[session_dirty]
    assert dirty_row.branch == f"session/{session_dirty}"
    assert dirty_row.commits_ahead == 0
    assert dirty_row.is_dirty is True
    assert dirty_row.ff_eligible is False
    assert dirty_row.clone_stale is False
    assert dirty_row.session_live is False
    assert dirty_row.dirty_files[0].status == "M"
    assert dirty_row.dirty_files[0].path == "README.md"

    clean_row = by_session[session_clean]
    assert clean_row.branch == f"session/{session_clean}"
    assert clean_row.commits_ahead == 0
    assert clean_row.is_dirty is False
    assert clean_row.ff_eligible is False
    assert clean_row.clone_stale is False
    assert clean_row.session_live is False


def test_scan_all_worktrees_suppresses_cross_worktree_duplicates(tmp_path, monkeypatch):
    """A commit checked out in a second worktree (review checkout) is listed
    once, on the session that owns the branch; the mirror row reports it in
    ``duplicate_commits`` and drops to zero pending."""
    session_author = "sess-author"
    session_review = "sess-review"
    worktrees_dir, _clone, wt_author = _make_writable_session_worktree(
        tmp_path, session_author, monkeypatch,
    )
    url = str(next(tmp_path.glob("upstream.git")))
    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj, session_review,
        repos_dir=tmp_path / "repos", worktrees_dir=worktrees_dir,
    )
    wt_review = worktrees_dir / session_review / "upstream"

    subprocess.run(["git", "-C", str(wt_author), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wt_author), "config", "user.name", "t"], check=True)
    (wt_author / "feature.txt").write_text("feature\n")
    subprocess.run(["git", "-C", str(wt_author), "add", "feature.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(wt_author), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "shared feature commit",
        ],
        check=True,
    )
    # Review checkout: same commit, different branch name, second worktree.
    subprocess.run(
        [
            "git", "-C", str(wt_review), "checkout", "-q", "-b",
            "review/1", f"session/{session_author}",
        ],
        check=True,
    )

    rows = wm.scan_all_worktrees(worktrees_dir=worktrees_dir, live_session_names=set())
    by_session = {row.session_name: row for row in rows}

    author_row = by_session[session_author]
    assert author_row.commits_ahead == 1
    assert author_row.commits[0].subject == "shared feature commit"
    assert author_row.duplicate_commits == []

    review_row = by_session[session_review]
    assert review_row.commits_ahead == 0
    assert review_row.commits == []
    assert review_row.ff_eligible is False
    assert len(review_row.duplicate_commits) == 1
    dup = review_row.duplicate_commits[0]
    assert dup.subject == "shared feature commit"
    assert dup.of_session == session_author
    assert dup.of_repo == "upstream"


def test_scan_all_worktrees_session_filter_skips_sessions(tmp_path, monkeypatch):
    """``session_filter`` scopes the sweep: filtered-out sessions produce
    no rows (and, upstream, keep their cached rows instead)."""
    session_a = "sess-in-scope"
    session_b = "sess-out-of-scope"
    worktrees_dir, _clone, _wt = _make_writable_session_worktree(
        tmp_path, session_a, monkeypatch,
    )
    url = str(next(tmp_path.glob("upstream.git")))
    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj, session_b,
        repos_dir=tmp_path / "repos", worktrees_dir=worktrees_dir,
    )

    rows = wm.scan_all_worktrees(
        worktrees_dir=worktrees_dir,
        live_session_names=set(),
        session_filter=lambda name: name == session_a,
    )

    assert [row.session_name for row in rows] == [session_a]


def test_scan_all_worktrees_suppresses_rebased_copy_duplicates(tmp_path, monkeypatch):
    """A commit copied to another session branch under a different SHA
    (cherry-pick/rebase) is still recognized as the same pending change
    via its patch-id and listed on exactly one row. Both rows own their
    session branch here, so the canonical-rank tiebreak applies: the most
    recent session (``sess-copy`` > ``sess-author``) keeps the listing —
    the newer branch is the more likely home of the work's current form."""
    session_author = "sess-author"
    session_copy = "sess-copy"
    worktrees_dir, _clone, wt_author = _make_writable_session_worktree(
        tmp_path, session_author, monkeypatch,
    )
    url = str(next(tmp_path.glob("upstream.git")))
    proj = ProjectConfig(
        id="w", name="w", description="", image="img", graph_project="gp",
        repos=(RepoMount(url=url, mount="/workspace/upstream", writable=True),),
    )
    wm.prepare_session_mounts(
        proj, session_copy,
        repos_dir=tmp_path / "repos", worktrees_dir=worktrees_dir,
    )
    wt_copy = worktrees_dir / session_copy / "upstream"

    for wt in (wt_author, wt_copy):
        subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"], check=True)

    (wt_author / "feature.txt").write_text("feature\n")
    subprocess.run(["git", "-C", str(wt_author), "add", "feature.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(wt_author), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "shared feature commit",
        ],
        check=True,
    )
    author_sha = subprocess.run(
        ["git", "-C", str(wt_author), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Copy onto the other session branch: different SHA, same patch-id.
    # Pin a distinct committer date so the copy can't SHA-collide with the
    # original (same tree, same parent, same message, same-second commit).
    subprocess.run(
        ["git", "-C", str(wt_copy), "-c", "commit.gpgsign=false",
         "cherry-pick", author_sha],
        check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "2036-01-01T00:00:00 +0000"},
    )
    copy_sha = subprocess.run(
        ["git", "-C", str(wt_copy), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert copy_sha != author_sha

    rows = wm.scan_all_worktrees(worktrees_dir=worktrees_dir, live_session_names=set())
    by_session = {row.session_name: row for row in rows}

    copy_row = by_session[session_copy]
    assert copy_row.commits_ahead == 1
    assert copy_row.duplicate_commits == []

    author_row = by_session[session_author]
    assert author_row.commits_ahead == 0
    assert author_row.commits == []
    assert len(author_row.duplicate_commits) == 1
    assert author_row.duplicate_commits[0].sha == author_sha
    assert author_row.duplicate_commits[0].of_session == session_copy


def test_scan_all_worktrees_suppresses_net_empty_branches(tmp_path, monkeypatch):
    """A branch whose commits cancel out (commit + revert) has nothing to
    land: the row reports ``net_empty`` and zero pending commits."""
    session = "sess-net-empty"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "spec.txt").write_text("draft spec\n")
    subprocess.run(["git", "-C", str(worktree), "add", "spec.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "add spec",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "revert", "--no-edit", "HEAD",
        ],
        check=True,
    )

    rows = wm.scan_all_worktrees(worktrees_dir=worktrees_dir, live_session_names=set())
    row = next(item for item in rows if item.session_name == session)

    assert row.net_empty is True
    assert row.commits == []
    assert row.commits_ahead == 0
    assert row.ff_eligible is False


def test_scan_all_worktrees_keeps_dirty_worktree_ff_eligible_when_commit_is_linear(tmp_path, monkeypatch):
    session = "sess-dirty-mergeable"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch, repo_name="autonomy",
    )
    upstream = next(tmp_path.glob("upstream.git"))
    target_repo = tmp_path / "autonomy"
    subprocess.run(["git", "clone", "-q", str(upstream), str(target_repo)], check=True)
    monkeypatch.setattr(wm, "REPO_ROOT", target_repo)

    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "mergeable.txt").write_text("ready\n")
    subprocess.run(["git", "-C", str(worktree), "add", "mergeable.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "mergeable commit",
        ],
        check=True,
    )
    (worktree / "runtime.log").write_text("local byproduct\n")

    rows = wm.scan_all_worktrees(worktrees_dir=worktrees_dir, live_session_names={session})
    row = next(item for item in rows if item.session_name == session and item.repo_name == "autonomy")

    assert row.commits_ahead == 1
    assert row.is_dirty is False
    assert row.ff_eligible is True
    assert row.rebase_required is False


def test_scan_all_worktrees_lists_nested_untracked_files_individually(tmp_path, monkeypatch):
    session = "sess-untracked"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )

    vendor_dir = worktree / "tools" / "dashboard" / "static" / "vendor" / "highlightjs"
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "highlight.min.js").write_text("hljs\n")
    (vendor_dir / "github-dark.min.css").write_text("css\n")
    (vendor_dir / "LICENSE").write_text("license\n")

    rows = wm.scan_all_worktrees(
        worktrees_dir=worktrees_dir,
        live_session_names=set(),
    )

    row = next(item for item in rows if item.session_name == session)
    assert row.is_dirty is False
    assert sorted(file.path for file in row.dirty_files) == [
        "tools/dashboard/static/vendor/highlightjs/LICENSE",
        "tools/dashboard/static/vendor/highlightjs/github-dark.min.css",
        "tools/dashboard/static/vendor/highlightjs/highlight.min.js",
    ]


def test_merge_session_worktree_fast_forwards_matching_checkout(tmp_path, monkeypatch):
    session = "sess-merge"
    worktrees_dir, clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch, repo_name="autonomy",
    )
    upstream = next(tmp_path.glob("upstream.git"))
    target_repo = tmp_path / "autonomy"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(target_repo)],
        check=True,
    )
    monkeypatch.setattr(wm, "REPO_ROOT", target_repo)

    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "ff.txt").write_text("ff-only\n")
    subprocess.run(["git", "-C", str(worktree), "add", "ff.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "ff-only merge",
        ],
        check=True,
    )
    worktree_head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    result = wm.merge_session_worktree(
        session, "autonomy", worktrees_dir=worktrees_dir,
    )

    target_head = subprocess.run(
        ["git", "-C", str(target_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert target_head == worktree_head
    assert result["commit"] == worktree_head
    assert result["message"] == "ff-only merge"
    assert result["target_repo"] == str(target_repo)
    clone_head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", f"refs/heads/{result['target_branch']}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert clone_head == worktree_head


def test_merge_session_worktree_ignores_dirty_files_when_commit_is_ff_eligible(tmp_path, monkeypatch):
    session = "sess-merge-dirty"
    worktrees_dir, clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch, repo_name="autonomy",
    )
    upstream = next(tmp_path.glob("upstream.git"))
    target_repo = tmp_path / "autonomy"
    subprocess.run(["git", "clone", "-q", str(upstream), str(target_repo)], check=True)
    monkeypatch.setattr(wm, "REPO_ROOT", target_repo)

    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "ff.txt").write_text("ff-only\n")
    subprocess.run(["git", "-C", str(worktree), "add", "ff.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "ff-only merge",
        ],
        check=True,
    )
    worktree_head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (worktree / "dashboard.log").write_text("runtime byproduct\n")

    result = wm.merge_session_worktree(
        session, "autonomy", worktrees_dir=worktrees_dir,
    )

    target_head = subprocess.run(
        ["git", "-C", str(target_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert target_head == worktree_head
    assert result["commit"] == worktree_head
    clone_head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", f"refs/heads/{result['target_branch']}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert clone_head == worktree_head


def test_merge_session_worktree_commit_fast_forwards_to_selected_commit(tmp_path, monkeypatch):
    session = "sess-merge-commit"
    worktrees_dir, clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch, repo_name="autonomy",
    )
    upstream = next(tmp_path.glob("upstream.git"))
    target_repo = tmp_path / "autonomy"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(target_repo)],
        check=True,
    )
    monkeypatch.setattr(wm, "REPO_ROOT", target_repo)

    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)

    (worktree / "first.txt").write_text("first\n")
    subprocess.run(["git", "-C", str(worktree), "add", "first.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "first commit",
        ],
        check=True,
    )
    first_sha = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    (worktree / "second.txt").write_text("second\n")
    subprocess.run(["git", "-C", str(worktree), "add", "second.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "second commit",
        ],
        check=True,
    )

    # Dirty files should be visible to the scanner but not part of the
    # selected commit merge operation.
    (worktree / "scratch.txt").write_text("uncommitted\n")

    detail = wm.get_session_worktree_commit_detail(
        session, "autonomy", first_sha[:7], worktrees_dir=worktrees_dir,
    )
    assert detail.subject == "first commit"
    assert "first.txt" in (detail.patch or "")

    result = wm.merge_session_worktree_commit(
        session, "autonomy", first_sha[:7], worktrees_dir=worktrees_dir,
    )

    target_head = subprocess.run(
        ["git", "-C", str(target_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert target_head == first_sha
    assert result["commit"] == first_sha
    assert result["message"] == "first commit"
    assert (target_repo / "first.txt").exists()
    assert not (target_repo / "second.txt").exists()
    clone_head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", f"refs/heads/{result['target_branch']}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert clone_head == first_sha


def test_get_session_worktree_commit_detail_accepts_commit_ahead_of_divergent_master(tmp_path, monkeypatch):
    session = "sess-divergent-master"
    worktrees_dir, clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch, repo_name="autonomy",
    )

    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "first.txt").write_text("first\n")
    subprocess.run(["git", "-C", str(worktree), "add", "first.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "first commit",
        ],
        check=True,
    )
    first_sha = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-B", "master", "origin/HEAD"], check=True)
    (clone / "README.md").write_text("divergent master\n")
    subprocess.run(["git", "-C", str(clone), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(clone), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "master diverged",
        ],
        check=True,
    )

    monkeypatch.setattr(wm, "_worktree_dashboard_base_ref", lambda _worktree, _repo_name: "master")

    detail = wm.get_session_worktree_commit_detail(
        session, "autonomy", first_sha[:7], worktrees_dir=worktrees_dir,
    )

    assert detail.sha == first_sha
    assert detail.subject == "first commit"

def test_scan_all_worktrees_hides_commits_already_in_target_branch(tmp_path, monkeypatch):
    """A commit merged into the target repo disappears once the clone syncs.

    The pending filter runs entirely in the worktree against the managed
    clone's integration ref. While the clone lags the target repo (the
    ``clone_stale`` window — the dashboard merge flow closes it by syncing
    the clone as part of the merge), the commit is conservatively still
    listed; after the sync it is gone from both the commit list and the
    ahead count.
    """
    session = "sess-already-merged"
    worktrees_dir, clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch, repo_name="autonomy",
    )
    upstream = next(tmp_path.glob("upstream.git"))
    target_repo = tmp_path / "autonomy"
    subprocess.run(["git", "clone", "-q", str(upstream), str(target_repo)], check=True)
    monkeypatch.setattr(wm, "REPO_ROOT", target_repo)

    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "merged.txt").write_text("already merged\n")
    subprocess.run(["git", "-C", str(worktree), "add", "merged.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "already merged",
        ],
        check=True,
    )
    merged_sha = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    session_branch = f"session/{session}"
    subprocess.run(["git", "-C", str(target_repo), "fetch", str(clone), session_branch], check=True)
    subprocess.run(["git", "-C", str(target_repo), "merge", "--ff-only", "FETCH_HEAD"], check=True)

    subprocess.run(["git", "-C", str(target_repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(target_repo), "config", "user.name", "t"], check=True)
    (target_repo / "README.md").write_text("target moved again\n")
    subprocess.run(["git", "-C", str(target_repo), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(target_repo), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "target moved again",
        ],
        check=True,
    )

    # Clone not yet synced from the target repo: conservatively still pending.
    rows = wm.scan_all_worktrees(
        worktrees_dir=worktrees_dir,
        live_session_names={session},
    )
    row = next(item for item in rows if item.session_name == session and item.repo_name == "autonomy")
    assert row.commits_ahead == 1
    assert [commit.sha for commit in row.commits] == [merged_sha]

    # Publish the merge to the repo's origin and refresh the managed clone
    # — the same fetch every session launch performs — closing the window.
    subprocess.run(["git", "-C", str(target_repo), "push", "-q", "origin", "main"], check=True)
    wm.ensure_managed_clone(str(upstream), repos_dir=tmp_path / "repos")

    rows = wm.scan_all_worktrees(
        worktrees_dir=worktrees_dir,
        live_session_names={session},
    )
    row = next(item for item in rows if item.session_name == session and item.repo_name == "autonomy")

    assert row.commits_ahead == 0
    assert row.commits == []
    assert row.ff_eligible is False

    with pytest.raises(wm.WorkspaceError, match="commit is not in worktree ahead range"):
        wm.get_session_worktree_commit_detail(
            session, "autonomy", merged_sha[:7], worktrees_dir=worktrees_dir,
        )

    with pytest.raises(wm.WorkspaceError, match="commit is not in worktree ahead range"):
        wm.merge_session_worktree_commit(
            session, "autonomy", merged_sha[:7], worktrees_dir=worktrees_dir,
        )

    # auto-24a60: get_repo_commit_detail bypasses the ahead-range
    # gate so the activity-feed Diff overlay can read commits that
    # have already been merged into master (and therefore aren't
    # "ahead" of any session worktree). Same SHA the gated reader
    # rejected above.
    detail = wm.get_repo_commit_detail(target_repo, merged_sha[:7])
    assert detail.sha == merged_sha
    assert detail.subject == "already merged"
    assert "merged.txt" in (detail.patch or "")


def test_scan_all_worktrees_marks_clone_stale_until_base_synced(tmp_path, monkeypatch):
    session = "sess-clone-stale"
    worktrees_dir, clone, _worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch, repo_name="autonomy",
    )
    upstream = next(tmp_path.glob("upstream.git"))
    target_repo = tmp_path / "autonomy"
    subprocess.run(["git", "clone", "-q", str(upstream), str(target_repo)], check=True)
    monkeypatch.setattr(wm, "REPO_ROOT", target_repo)

    subprocess.run(["git", "-C", str(target_repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(target_repo), "config", "user.name", "t"], check=True)
    (target_repo / "README.md").write_text("target advanced\n")
    subprocess.run(["git", "-C", str(target_repo), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(target_repo), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "target advanced",
        ],
        check=True,
    )
    target_head = subprocess.run(
        ["git", "-C", str(target_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    rows = wm.scan_all_worktrees(worktrees_dir=worktrees_dir, live_session_names={session})
    row = next(item for item in rows if item.session_name == session and item.repo_name == "autonomy")
    assert row.clone_stale is True
    assert row.ff_eligible is False

    sync_result = wm.sync_session_worktree_base(session, "autonomy", worktrees_dir=worktrees_dir)
    assert sync_result["target_branch"] in {"main", "master"}
    clone_head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", f"refs/heads/{sync_result['target_branch']}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert clone_head == target_head

    rows = wm.scan_all_worktrees(worktrees_dir=worktrees_dir, live_session_names={session})
    row = next(item for item in rows if item.session_name == session and item.repo_name == "autonomy")
    assert row.clone_stale is False


def test_merge_session_worktree_commit_reports_rebase_required_details(tmp_path, monkeypatch):
    session = "sess-rebase-required"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch, repo_name="autonomy",
    )
    upstream = next(tmp_path.glob("upstream.git"))
    target_repo = tmp_path / "autonomy"
    subprocess.run(["git", "clone", "-q", str(upstream), str(target_repo)], check=True)
    monkeypatch.setattr(wm, "REPO_ROOT", target_repo)
    monkeypatch.setattr(wm, "_live_session_names", lambda: {session})

    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "feature.txt").write_text("feature\n")
    subprocess.run(["git", "-C", str(worktree), "add", "feature.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "feature work",
        ],
        check=True,
    )
    feature_sha = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (worktree / "scratch.txt").write_text("dirty\n")

    subprocess.run(["git", "-C", str(target_repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(target_repo), "config", "user.name", "t"], check=True)
    (target_repo / "README.md").write_text("target advanced\n")
    subprocess.run(["git", "-C", str(target_repo), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(target_repo), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "target advanced",
        ],
        check=True,
    )

    wm.sync_session_worktree_base(session, "autonomy", worktrees_dir=worktrees_dir)
    rows = wm.scan_all_worktrees(worktrees_dir=worktrees_dir, live_session_names={session})
    row = next(item for item in rows if item.session_name == session and item.repo_name == "autonomy")
    assert row.rebase_required is True
    assert row.ff_eligible is False
    assert row.is_dirty is False
    info = wm.get_session_worktree_rebase_info(session, "autonomy", worktrees_dir=worktrees_dir)
    assert info["target_branch"] in {"main", "master"}
    assert info["commits_behind"] == 1
    assert info["session_live"] is True
    assert info["commit"] == feature_sha
    assert info["is_dirty"] is False

    with pytest.raises(wm.RebaseRequiredError) as excinfo:
        wm.merge_session_worktree_commit(
            session, "autonomy", feature_sha[:7], worktrees_dir=worktrees_dir,
        )

    assert excinfo.value.commits_behind == 1
    assert excinfo.value.session_live is True
    assert excinfo.value.target_branch in {"main", "master"}
def test_cleanup_session_worktree_rejects_live_session(tmp_path, monkeypatch):
    session = "sess-live"
    worktrees_dir, _clone, _worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    monkeypatch.setattr(wm, "_live_session_names", lambda: {session})

    with pytest.raises(wm.WorkspaceError, match="cannot discard live worktree"):
        wm.cleanup_session_worktree(
            session, "upstream", worktrees_dir=worktrees_dir, force=True,
        )


def test_merge_session_worktree_rejects_non_autonomy_repo(tmp_path, monkeypatch):
    session = "sess-merge-unsupported"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )

    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    (worktree / "ff.txt").write_text("ff-only\n")
    subprocess.run(["git", "-C", str(worktree), "add", "ff.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "ff-only unsupported",
        ],
        check=True,
    )

    with pytest.raises(wm.WorkspaceError, match="only 'autonomy'"):
        wm.merge_session_worktree(
            session, "upstream", worktrees_dir=worktrees_dir,
        )


# ── Phase 0 perf redesign (auto-0peos): batched git calls ──────────


def _run(args, cwd):
    # Neutralize GPG signing for every fixture git op, not just `commit`:
    # `cherry-pick` also creates commits, and a host with global
    # commit.gpgsign=true fails the fixture before the code under test runs.
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "commit.gpgsign=false",
            "-c", "gpg.program=/bin/true",
            *args,
        ],
        check=True,
    )


def _rev_parse(cwd, ref="HEAD"):
    return subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", ref],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _make_cherry_pick_fixture(tmp_path):
    """One repo, ``master`` + ``feature``, with all three merge dispositions.

    Returns ``(repo, merged_sha, cherry_sha, pending_sha)`` where, relative
    to ``master``:
      - ``merged_sha`` was fast-forward merged (SHA-identical ancestor).
      - ``cherry_sha`` was cherry-picked onto master with a *different* SHA
        but the same patch-id.
      - ``pending_sha`` never landed on master at all.
    """
    repo = tmp_path / "cherry-fixture"
    repo.mkdir()
    _run(["init", "-q", "-b", "master"], repo)
    _run(["config", "user.email", "t@t"], repo)
    _run(["config", "user.name", "t"], repo)
    (repo / "base.txt").write_text("base\n")
    _run(["add", "base.txt"], repo)
    _run(["commit", "-q", "-m", "base"], repo)

    _run(["checkout", "-q", "-b", "feature"], repo)
    (repo / "merged.txt").write_text("merged\n")
    _run(["add", "merged.txt"], repo)
    _run(["commit", "-q", "-m", "merged commit"], repo)
    merged_sha = _rev_parse(repo)

    (repo / "cherry.txt").write_text("cherry\n")
    _run(["add", "cherry.txt"], repo)
    _run(["commit", "-q", "-m", "cherry commit"], repo)
    cherry_sha = _rev_parse(repo)

    (repo / "pending.txt").write_text("pending\n")
    _run(["add", "pending.txt"], repo)
    _run(["commit", "-q", "-m", "pending commit"], repo)
    pending_sha = _rev_parse(repo)

    _run(["checkout", "-q", "master"], repo)
    _run(["merge", "-q", "--ff-only", merged_sha], repo)
    _run(["cherry-pick", cherry_sha], repo)
    (repo / "mastermove.txt").write_text("mastermove\n")
    _run(["add", "mastermove.txt"], repo)
    _run(["commit", "-q", "-m", "mastermove"], repo)

    _run(["checkout", "-q", "feature"], repo)
    return repo, merged_sha, cherry_sha, pending_sha


def test_cherry_unmerged_shas_classifies_all_dispositions(tmp_path):
    """``_cherry_unmerged_shas`` covers all three merge dispositions:
    SHA-identical ancestor (fast-forwarded) and patch-id equivalent under
    a different SHA (cherry-picked) are both absent from the ``+`` set;
    the genuinely pending commit is present."""
    repo, merged_sha, cherry_sha, pending_sha = _make_cherry_pick_fixture(tmp_path)

    unmerged = wm._cherry_unmerged_shas(repo, "master")

    assert unmerged == {pending_sha}


def test_dashboard_pending_commit_shas_filters_in_the_worktree(tmp_path):
    """The merged filter must work with ``REPO_ROOT`` pointing anywhere.

    Regression: this filter used to run ``git cherry`` in the host
    checkout (``REPO_ROOT``), which does not contain session-branch
    objects — the command failed on unknown SHAs and the filter silently
    kept already-merged commits listed forever. Note: NO monkeypatch of
    ``REPO_ROOT`` here; the real one has no relation to the fixture repo,
    exactly like production.
    """
    repo, merged_sha, cherry_sha, pending_sha = _make_cherry_pick_fixture(tmp_path)

    pending = wm._dashboard_pending_commit_shas(repo, "autonomy", base_ref="master")
    assert pending == [pending_sha]


def test_dashboard_pending_commit_shas_filters_non_autonomy_repos(tmp_path):
    """The patch-id merged filter applies to every repo, not just autonomy."""
    repo, merged_sha, cherry_sha, pending_sha = _make_cherry_pick_fixture(tmp_path)

    pending = wm._dashboard_pending_commit_shas(repo, "enterprise_ng", base_ref="master")
    assert pending == [pending_sha]


def _make_commit_detail_fixture(tmp_path):
    """A repo with an add, a modify, a rename, and a binary-file commit."""
    repo = tmp_path / "detail-fixture"
    repo.mkdir()
    _run(["init", "-q", "-b", "master"], repo)
    _run(["config", "user.email", "t@t"], repo)
    _run(["config", "user.name", "t"], repo)
    (repo / "base.txt").write_text("base\n")
    _run(["add", "base.txt"], repo)
    _run(["commit", "-q", "-m", "base"], repo)

    shas = []

    (repo / "add.txt").write_text("hello\n")
    _run(["add", "add.txt"], repo)
    _run(
        ["-c", "commit.gpgsign=false", "commit", "-q", "-m",
         "add file\n\nwith a multi-line body\nsecond line"],
        repo,
    )
    shas.append(_rev_parse(repo))

    (repo / "add.txt").write_text("hello world\n")
    _run(["add", "add.txt"], repo)
    _run(["commit", "-q", "-m", "modify file"], repo)
    shas.append(_rev_parse(repo))

    _run(["mv", "add.txt", "renamed.txt"], repo)
    _run(["commit", "-q", "-m", "rename file"], repo)
    shas.append(_rev_parse(repo))

    (repo / "blob.bin").write_bytes(bytes(range(256)))
    _run(["add", "blob.bin"], repo)
    _run(["commit", "-q", "-m", "add binary"], repo)
    shas.append(_rev_parse(repo))

    return repo, shas


def test_read_worktree_commits_batch_matches_per_commit_reads(tmp_path):
    """The batched ``git log`` parser must match the old per-commit reads
    exactly, quirks (rename numstat mismatch) included — this is a
    data-source change only, not a parsing-behavior change."""
    repo, shas = _make_commit_detail_fixture(tmp_path)

    expected = [wm._read_worktree_commit(repo, sha) for sha in shas]
    batch = wm._read_worktree_commits_batch(repo, shas)
    actual = [batch[sha] for sha in shas]

    assert actual == expected
    assert any(f.status == "R" for commit in actual for f in commit.files)
    assert any(f.path == "blob.bin" for commit in actual for f in commit.files)


def test_cleanup_preserve_verdict_demotes_repeat_warning_to_debug(tmp_path, monkeypatch, caplog):
    """First preserve verdict for a worktree logs WARNING; an unchanged
    repeat (same path, same reason) logs DEBUG instead — the fix for the
    45-line WARNING burst every prune tick on a stable set of old
    worktrees."""
    session = "sess-preserve-repeat"
    worktrees_dir, _clone, worktree = _make_writable_session_worktree(
        tmp_path, session, monkeypatch,
    )
    (worktree / "README.md").write_text("edited but not committed\n")

    caplog.set_level(logging.DEBUG, logger="agents.workspace_manager")

    wm.cleanup_session_worktrees(session, worktrees_dir=worktrees_dir)
    first_preserve = [
        r for r in caplog.records
        if "preserving" in r.getMessage()
    ]
    assert len(first_preserve) == 1
    assert first_preserve[0].levelno == logging.WARNING

    caplog.clear()

    wm.cleanup_session_worktrees(session, worktrees_dir=worktrees_dir)
    second_preserve = [
        r for r in caplog.records
        if "preserving" in r.getMessage()
    ]
    assert len(second_preserve) == 1
    assert second_preserve[0].levelno == logging.DEBUG
