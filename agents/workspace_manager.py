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
    - managed clone → its own absolute host path (rw) so the worktree's
      ``.git`` file (which uses absolute paths) resolves inside the container.
      The clone must be rw because ``git add``/``commit`` in the worktree
      writes into ``<clone>/.git/worktrees/<name>/`` (index, HEAD, refs) and
      into the clone's shared object store.

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
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

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
            # managed clone — mount the clone at that same path (rw) so the
            # container can resolve it and so ``git add``/``commit`` can
            # write the worktree's per-worktree git state (index, refs) that
            # lives at ``<clone>/.git/worktrees/<name>/``.
            mounts[str(clone)] = str(clone)
        else:
            _update_readonly_clone(clone)
            mounts[str(clone)] = f"{repo.mount}:ro"
    return mounts


# ── Session teardown ──────────────────────────────────────────────

# Branch name prefix used by ``create_worktree`` in ``prepare_session_mounts``.
SESSION_BRANCH_PREFIX = "session/"


@dataclass
class CleanupResult:
    """Outcome of a session-worktree cleanup pass."""

    removed: list[str] = field(default_factory=list)
    preserved: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.removed or self.preserved or self.errors)


def _git_output(args: list[str], cwd: Path, *, timeout: int = 15) -> tuple[int, str, str]:
    """Run git and return (rc, stdout, stderr); never raises on non-zero exit."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)
    return r.returncode, r.stdout, r.stderr


def _find_managed_clone_for_worktree(worktree: Path) -> Path | None:
    """Read ``.git`` in a worktree to find its managed clone.

    A worktree's ``.git`` is a file containing ``gitdir: <path>/worktrees/<name>``.
    The managed clone is two directories up from that ``worktrees/<name>`` leaf.
    """
    dotgit = worktree / ".git"
    if not dotgit.exists():
        return None
    try:
        content = dotgit.read_text().strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    gitdir = Path(content.split(":", 1)[1].strip())
    # e.g. <clone>/worktrees/<name>  →  <clone>
    parts = gitdir.parts
    if len(parts) >= 2 and parts[-2] == "worktrees":
        return gitdir.parent.parent
    return None


def _worktree_has_uncommitted_changes(worktree: Path) -> bool:
    rc, out, _ = _git_output(["status", "--porcelain"], worktree, timeout=15)
    if rc != 0:
        # If status fails, treat as "dirty" — err on the side of preserving.
        return True
    return bool(out.strip())


def _worktree_has_unpushed_commits(worktree: Path) -> bool:
    """Return True if HEAD has commits not reachable from ``origin/HEAD``.

    If the comparison can't be made (missing upstream), returns True so
    we preserve by default.
    """
    rc, out, _ = _git_output(
        ["rev-list", "--count", "origin/HEAD..HEAD"],
        worktree,
        timeout=15,
    )
    if rc != 0:
        return True
    try:
        return int(out.strip() or "0") > 0
    except ValueError:
        return True


def _delete_branch(clone: Path, branch: str) -> None:
    """Delete ``branch`` from ``clone``; best-effort, logged on failure."""
    rc, _, err = _git_output(["branch", "-D", branch], clone, timeout=15)
    if rc != 0 and "not found" not in err.lower():
        logger.warning(
            "workspace cleanup: failed to delete branch %s in %s: %s",
            branch, clone, err.strip(),
        )


def _worktree_remove(clone: Path, worktree: Path) -> tuple[bool, str]:
    """``git worktree remove --force`` from the managed clone. Returns (ok, err)."""
    rc, _, err = _git_output(
        ["worktree", "remove", str(worktree), "--force"],
        clone,
        timeout=30,
    )
    return rc == 0, err.strip()


def _worktree_prune(clone: Path) -> None:
    """Prune stale worktree metadata in the managed clone."""
    _git_output(["worktree", "prune"], clone, timeout=15)


def cleanup_session_worktrees(
    session_name: str,
    *,
    force: bool = False,
    worktrees_dir: Path = WORKTREES_DIR,
) -> CleanupResult:
    """Remove worktrees and branch for ``session_name``.

    Walks ``data/worktrees/{session_name}/`` and, for each repo subdirectory:

    - If the worktree has uncommitted changes or unpushed commits on its
      ``session/{session_name}`` branch, preserve it (unless ``force=True``)
      and record the reason.
    - Otherwise run ``git worktree remove --force`` against the managed
      clone, delete the session branch, and prune the clone's worktree
      metadata.

    The containing ``{session_name}`` directory is removed once empty.
    Returns a :class:`CleanupResult` summarizing what was done.
    """
    result = CleanupResult()
    session_dir = worktrees_dir / session_name
    if not session_dir.exists():
        return result
    if not session_dir.is_dir():
        result.errors.append((str(session_dir), "not a directory"))
        return result

    branch = f"{SESSION_BRANCH_PREFIX}{session_name}"
    clones_touched: set[Path] = set()

    for entry in sorted(session_dir.iterdir()):
        if not entry.is_dir():
            continue
        clone = _find_managed_clone_for_worktree(entry)

        if not force:
            reasons: list[str] = []
            if _worktree_has_uncommitted_changes(entry):
                reasons.append("uncommitted changes")
            elif _worktree_has_unpushed_commits(entry):
                # Only check commits when the tree is clean — avoids
                # treating a mid-edit worktree as "unpushed".
                reasons.append("unpushed commits")
            if reasons:
                result.preserved.append((str(entry), ", ".join(reasons)))
                logger.warning(
                    "workspace cleanup: preserving %s (%s)",
                    entry, ", ".join(reasons),
                )
                continue

        if clone is None:
            # Stale worktree with no resolvable clone — remove the directory.
            try:
                shutil.rmtree(entry)
                result.removed.append(str(entry))
            except OSError as e:
                result.errors.append((str(entry), f"rmtree: {e}"))
            continue

        ok, err = _worktree_remove(clone, entry)
        if not ok:
            # ``git worktree remove`` can fail if the clone's metadata is
            # out of sync with the filesystem; fall back to rmtree + prune.
            if entry.exists():
                try:
                    shutil.rmtree(entry)
                except OSError as e:
                    result.errors.append((str(entry), f"remove failed: {err}; rmtree: {e}"))
                    continue
            logger.info(
                "workspace cleanup: 'worktree remove' failed for %s (%s); "
                "fell back to rmtree",
                entry, err,
            )

        result.removed.append(str(entry))
        clones_touched.add(clone)

    # Drop the now-empty session directory (may still hold files if errors)
    if session_dir.exists():
        try:
            remaining = [p for p in session_dir.iterdir()]
        except OSError:
            remaining = []
        if not remaining:
            try:
                session_dir.rmdir()
            except OSError as e:
                result.errors.append((str(session_dir), f"rmdir: {e}"))

    # Delete the session branch and prune stale worktree metadata in each
    # managed clone we touched. Both are best-effort — a failure here does
    # not roll back the removed worktrees.
    for clone in clones_touched:
        _delete_branch(clone, branch)
        _worktree_prune(clone)

    return result


def prune_orphan_worktrees(
    live_session_names: Iterable[str],
    *,
    force: bool = False,
    worktrees_dir: Path = WORKTREES_DIR,
) -> dict[str, CleanupResult]:
    """Clean worktrees for sessions no longer in ``live_session_names``.

    Scans ``worktrees_dir`` and calls :func:`cleanup_session_worktrees` for
    each subdirectory whose name is not in ``live_session_names``.
    Returns a mapping of ``session_name → CleanupResult``.
    """
    if not worktrees_dir.exists():
        return {}
    live = set(live_session_names)
    results: dict[str, CleanupResult] = {}
    for entry in sorted(worktrees_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in live:
            continue
        results[entry.name] = cleanup_session_worktrees(
            entry.name, force=force, worktrees_dir=worktrees_dir,
        )
    return results
