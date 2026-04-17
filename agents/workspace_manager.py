"""Per-session workspace preparation — managed clones and worktrees.

Given a ProjectConfig and a session name, this module:

1. Ensures a managed clone of each repo URL exists under ``data/repos/``.
2. Runs ``git fetch origin --prune`` on every clone.
3. For writable repos, creates a per-session worktree under
   ``data/worktrees/{session_name}/`` on a fresh ``session/{session_name}``
   branch based on ``origin/HEAD``.
4. Returns a mount spec dict for ``agents.session_launcher.launch_session``.

Mount layout for a writable repo:
    - worktree → container mount path (rw)
    - managed clone → its own absolute host path (ro) so the worktree's
      ``.git`` file (which uses absolute paths) resolves inside the container.

Read-only repos are checked out to ``origin/HEAD`` in the managed clone itself
and mounted directly at the container mount path.

SSH credentials for ``git clone``/``git fetch`` come from the host user's
environment (SSH agent or ~/.ssh keys) — the dashboard server runs on the
host, not in a container.

Design refs:
    graph://e9448254-18f  Pluggable project-specific container sessions
    graph://eabec73c-baa  Workspaces & Orgs signpost
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from agents.project_config import ProjectConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
REPOS_DIR = DATA_DIR / "repos"
WORKTREES_DIR = DATA_DIR / "worktrees"

logger = logging.getLogger(__name__)


class WorkspaceError(RuntimeError):
    """Raised when repo clone, fetch, or worktree operations fail."""


_SSH_RE = re.compile(r"^(?P<user>[\w.-]+)@(?P<host>[\w.-]+):(?P<path>.+?)/?$")
_URL_RE = re.compile(r"^(?:https?|ssh|git)://(?:[\w.-]+@)?(?P<host>[\w.-]+)(?::\d+)?/(?P<path>.+?)/?$")


def parse_repo_url(url: str) -> tuple[str, str]:
    """Parse a git URL into (host, path) where path has no trailing ``.git``.

    Examples::

        git@github.com:anchore/enterprise.git  →  ("github.com", "anchore/enterprise")
        https://github.com/foo/bar.git         →  ("github.com", "foo/bar")
    """
    m = _SSH_RE.match(url)
    if not m:
        m = _URL_RE.match(url)
    if not m:
        raise WorkspaceError(f"unrecognized git URL: {url!r}")
    path = m.group("path")
    if path.endswith(".git"):
        path = path[:-4]
    return m.group("host"), path


def managed_clone_path(url: str, *, repos_dir: Path = REPOS_DIR) -> Path:
    """Return the filesystem path where ``url`` is cloned under ``repos_dir``."""
    host, path = parse_repo_url(url)
    return repos_dir / host / f"{path}.git"


def _run_git(args: list[str], *, cwd: Path | None = None, timeout: int = 600) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise WorkspaceError(f"git {' '.join(args)} timed out after {timeout}s")
    if result.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed "
            f"(cwd={cwd}, rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def ensure_managed_clone(url: str, *, repos_dir: Path = REPOS_DIR) -> Path:
    """Clone ``url`` under ``repos_dir`` if missing, otherwise fetch + prune.

    Returns the path to the managed clone. Subsequent calls for the same URL
    are idempotent (just a fetch).
    """
    clone_path = managed_clone_path(url, repos_dir=repos_dir)
    if clone_path.exists():
        logger.info("workspace: fetching %s", clone_path)
        _run_git(["fetch", "origin", "--prune"], cwd=clone_path)
    else:
        logger.info("workspace: cloning %s → %s", url, clone_path)
        clone_path.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", url, str(clone_path)])
    return clone_path


def _worktree_basename(url: str) -> str:
    _host, path = parse_repo_url(url)
    return path.rsplit("/", 1)[-1]


def create_worktree(managed_clone: Path, worktree_dir: Path, branch: str) -> Path:
    """Create a new worktree at ``worktree_dir`` on ``branch`` from ``origin/HEAD``.

    If the worktree already exists it is reused as-is.
    """
    if worktree_dir.exists():
        return worktree_dir
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["worktree", "add", "-b", branch, str(worktree_dir), "origin/HEAD"],
        cwd=managed_clone,
    )
    return worktree_dir


def _update_readonly_clone(clone: Path) -> None:
    """Fast-forward the managed clone's working tree to ``origin/HEAD``.

    Read-only repos are mounted directly from the managed clone, so the
    clone's own checkout must be current. We use ``checkout --detach`` so
    the clone stays on a detached HEAD and never conflicts with worktrees.
    """
    _run_git(["checkout", "--detach", "origin/HEAD"], cwd=clone)


def prepare_session_mounts(
    project: ProjectConfig,
    session_name: str,
    *,
    repos_dir: Path = REPOS_DIR,
    worktrees_dir: Path = WORKTREES_DIR,
) -> dict[str, str]:
    """Prepare clones + worktrees for ``project`` and return launch_session mounts.

    The returned dict maps host paths to ``container_path[:mode]`` strings,
    suitable for ``launch_session(mounts=...)``.
    """
    mounts: dict[str, str] = {}
    for repo in project.repos:
        clone = ensure_managed_clone(repo.url, repos_dir=repos_dir)
        if repo.writable:
            worktree = worktrees_dir / session_name / _worktree_basename(repo.url)
            create_worktree(clone, worktree, f"session/{session_name}")
            mounts[str(worktree)] = repo.mount
            # Worktree's .git file points at an absolute host path inside the
            # managed clone — mount the clone at that same path (ro) so the
            # container can resolve it.
            mounts[str(clone)] = f"{clone}:ro"
        else:
            _update_readonly_clone(clone)
            mounts[str(clone)] = f"{repo.mount}:ro"
    return mounts
