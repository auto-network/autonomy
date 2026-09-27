"""The platform snapshot works when the node's checkout is on a detached HEAD.

Release 2026.09.27-2968acc7 was built from a checkout of a commit, not a
branch. Its image's /app clones with no default branch and no origin/HEAD, so
``_update_readonly_clone`` ran ``git checkout --detach origin/HEAD`` and failed,
and every Getting Started session on the Windows test node launched without
/workspace/repo: ``graph`` failed with "No module named 'tools'". The snapshot
now falls back to the exact commit the checkout is on.

Real git in tmp directories; nothing touches the live checkout.
"""

import subprocess
from pathlib import Path

from agents import session_launcher, workspace_manager


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _source(tmp_path: Path, *, detached: bool) -> Path:
    src = tmp_path / "app"
    src.mkdir()
    _git(src, "init", "-q", "-b", "master")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "t")
    (src / "tools").mkdir()
    (src / "tools" / "__init__.py").write_text("")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "one")
    if detached:
        _git(src, "checkout", "-q", "--detach", "HEAD")
        _git(src, "branch", "-q", "-D", "master")
    return src


def _point_at(monkeypatch, tmp_path: Path, src: Path) -> None:
    monkeypatch.setattr(session_launcher, "REPO_ROOT", src)
    real = workspace_manager.ensure_managed_clone
    monkeypatch.setattr(
        workspace_manager, "ensure_managed_clone",
        lambda url, **kw: real(url, repos_dir=tmp_path / "repos", **kw),
    )


def test_snapshot_of_a_detached_checkout_is_its_commit(monkeypatch, tmp_path):
    src = _source(tmp_path, detached=True)
    _point_at(monkeypatch, tmp_path, src)
    snap = session_launcher._ensure_platform_snapshot()
    assert snap is not None, "launch would go ahead without /workspace/repo"
    assert _git(Path(snap), "rev-parse", "HEAD") == _git(src, "rev-parse", "HEAD")
    assert (Path(snap) / "tools" / "__init__.py").is_file()


def test_snapshot_of_a_branch_checkout_is_unchanged(monkeypatch, tmp_path):
    src = _source(tmp_path, detached=False)
    _point_at(monkeypatch, tmp_path, src)
    snap = session_launcher._ensure_platform_snapshot()
    assert snap is not None
    assert _git(Path(snap), "rev-parse", "HEAD") == _git(src, "rev-parse", "HEAD")
