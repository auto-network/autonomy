"""Per-session workspace preparation — managed clones and worktrees.

Given a WorkspaceV1 and a session name, this module:

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

from agents.workspace_settings import (
    WorkspaceV1,
    WorkspaceMountInvalidError,
    WorkspaceMountMissingError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
REPOS_DIR = DATA_DIR / "repos"
WORKTREES_DIR = DATA_DIR / "worktrees"

logger = logging.getLogger(__name__)


class WorkspaceError(RuntimeError):
    """Raised when repo clone, fetch, or worktree operations fail."""


class RebaseRequiredError(WorkspaceError):
    """Raised when a selected commit requires rebase before dashboard merge."""

    def __init__(
        self,
        *,
        target_branch: str,
        commits_behind: int,
        fork_sha: str,
        session_live: bool,
    ) -> None:
        noun = "commit" if commits_behind == 1 else "commits"
        super().__init__(f"Parent has advanced {commits_behind} {noun}, rebase required before merge.")
        self.target_branch = target_branch
        self.commits_behind = commits_behind
        self.fork_sha = fork_sha
        self.session_live = session_live


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
    workspace: WorkspaceV1,
    session_name: str,
    *,
    repos_dir: Path = REPOS_DIR,
    worktrees_dir: Path = WORKTREES_DIR,
) -> dict[str, str]:
    """Prepare clones + worktrees for ``workspace`` and return launch_session mounts.

    The returned dict maps host paths to ``container_path[:mode]`` strings,
    suitable for ``launch_session(mounts=...)``.
    """
    mounts: dict[str, str] = {}
    for repo in workspace.repos:
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
    _apply_workspace_mount_settings(workspace, mounts)
    return mounts


def _apply_workspace_mount_settings(
    workspace: WorkspaceV1, mounts: dict[str, str],
) -> None:
    """Extend *mounts* with ``autonomy.workspace.mount#1`` host directories.

    Required mounts whose host path is missing raise
    :class:`WorkspaceMountMissingError`; optional mounts are silently
    skipped (logged at DEBUG). A host path that exists but is not a
    directory raises :class:`WorkspaceMountInvalidError`.
    """
    for key, rs in workspace.mounts.items():
        payload = rs.payload
        host = Path(payload.host_path)
        if not host.exists():
            if payload.required:
                raise WorkspaceMountMissingError(
                    mount_key=key,
                    origin_org=rs.org,
                    state=rs.state,
                    host_path=payload.host_path,
                    container_path=payload.container_path,
                )
            logger.debug(
                "workspace: optional mount %s host_path %s absent — skipping",
                key, payload.host_path,
            )
            continue
        if not host.is_dir():
            raise WorkspaceMountInvalidError(
                mount_key=key,
                reason=f"host_path is not a directory: {payload.host_path}",
            )
        mounts[str(host)] = f"{payload.container_path}:{payload.mode}"


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


@dataclass(frozen=True)
class GitFileChange:
    """A file-level git change surfaced in the worktree dashboard."""

    status: str
    path: str
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class WorktreeCommit:
    """A committed change waiting on a session worktree branch."""

    sha: str
    short_sha: str
    subject: str
    author: str
    date: str
    body: str
    files: list[GitFileChange] = field(default_factory=list)
    patch: str | None = None


@dataclass(frozen=True)
class WorktreeState:
    """Current state of a session worktree for dashboard inspection."""

    session_name: str
    repo_name: str
    worktree_path: Path
    managed_clone: Path | None
    branch: str | None
    commits_ahead: int
    is_dirty: bool
    ff_eligible: bool
    clone_stale: bool
    session_live: bool
    commits: list[WorktreeCommit] = field(default_factory=list)
    dirty_files: list[GitFileChange] = field(default_factory=list)


@dataclass(frozen=True)
class WorktreeDirtyDetail:
    """Uncommitted file detail for a worktree review screen."""

    files: list[GitFileChange] = field(default_factory=list)
    patch: str | None = None


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
    # e.g. <clone>/.git/worktrees/<name>  →  <clone>
    # Older/bare layouts may omit ".git"; in that case the parent is already
    # the clone root.
    parts = gitdir.parts
    if len(parts) >= 2 and parts[-2] == "worktrees":
        clone_git_dir = gitdir.parent.parent
        if clone_git_dir.name == ".git":
            return clone_git_dir.parent
        return clone_git_dir
    return None


def _worktree_branch_name(worktree: Path) -> str | None:
    """Return the current branch name, or None for detached / unreadable HEAD."""
    rc, out, _ = _git_output(["rev-parse", "--abbrev-ref", "HEAD"], worktree, timeout=15)
    if rc != 0:
        return None
    branch = out.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _worktree_merge_base_ref(worktree: Path) -> str | None:
    """Return the preferred base ref for ahead/ff checks.

    The dashboard feature targets ``master`` per bead spec, but tests and
    some local clones still default to ``main`` with only ``origin/HEAD``
    available. Prefer ``master`` when present and fall back to ``origin/HEAD``.
    """
    for ref in ("master", "origin/HEAD"):
        rc, _, _ = _git_output(["rev-parse", "--verify", ref], worktree, timeout=15)
        if rc == 0:
            return ref
    return None


def _repo_default_branch(repo: Path) -> str | None:
    """Return the preferred local integration branch name for ``repo``."""
    for branch in ("main", "master"):
        rc, _, _ = _git_output(["rev-parse", "--verify", f"refs/heads/{branch}"], repo, timeout=15)
        if rc == 0:
            return branch

    rc, out, _ = _git_output(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo, timeout=15)
    if rc == 0:
        ref = out.strip()
        if ref:
            return ref.rsplit("/", 1)[-1]
    return None


def _repo_branch_head(repo: Path, branch: str) -> str | None:
    """Return the local branch SHA for ``repo``/``branch`` or None."""
    rc, out, _ = _git_output(["rev-parse", "--verify", f"refs/heads/{branch}"], repo, timeout=15)
    if rc != 0:
        return None
    head = out.strip()
    return head or None


def _autonomy_target_branch_and_head() -> tuple[str | None, str | None]:
    """Return the host-side autonomy integration branch and current HEAD SHA."""
    branch = _repo_default_branch(REPO_ROOT)
    if branch is None:
        return None, None
    return branch, _repo_branch_head(REPO_ROOT, branch)


def _sync_managed_clone_branch_ref(clone: Path, source_repo: Path, branch: str) -> None:
    """Sync ``clone``'s local branch ref from ``source_repo`` using a temp ref.

    The managed clone can have the destination branch checked out, so fetch into
    a temporary ref first and then advance the local branch with ``update-ref``.
    """
    temp_ref = "refs/tmp_sync"
    try:
        _run_git(
            ["fetch", str(source_repo), f"refs/heads/{branch}:{temp_ref}"],
            cwd=clone,
        )
        _run_git(
            ["update-ref", f"refs/heads/{branch}", temp_ref],
            cwd=clone,
        )
    finally:
        _git_output(["update-ref", "-d", temp_ref], clone, timeout=15)


def worktree_target_branch_name(
    session_name: str,
    repo_name: str,
    branch: str | None,
) -> str:
    """Return the dashboard target-branch label for a worktree."""
    target_branch, _target_head = _autonomy_target_branch_and_head()
    default_branch = target_branch or "main"
    if repo_name == "autonomy":
        return default_branch
    if branch and branch != f"{SESSION_BRANCH_PREFIX}{session_name}":
        return branch
    return default_branch


def _worktree_dashboard_base_ref(worktree: Path, repo_name: str) -> str | None:
    """Return the review base ref used by the dashboard for a worktree."""
    fallback = _worktree_merge_base_ref(worktree)
    if repo_name != "autonomy":
        return fallback

    _target_branch, target_head = _autonomy_target_branch_and_head()
    if not target_head:
        return fallback

    rc, _, _ = _git_output(["merge-base", "--is-ancestor", target_head, "HEAD"], worktree, timeout=15)
    if rc == 0:
        return target_head
    return fallback


def _worktree_commits_ahead(worktree: Path, *, base_ref: str | None = None) -> int:
    """Count commits reachable from HEAD but not from the merge base ref."""
    base_ref = base_ref or _worktree_merge_base_ref(worktree)
    if base_ref is None:
        return 0
    rc, out, _ = _git_output(["rev-list", "--count", f"{base_ref}..HEAD"], worktree, timeout=15)
    if rc != 0:
        return 0
    try:
        return int(out.strip() or "0")
    except ValueError:
        return 0


def _worktree_ff_only_safe(worktree: Path, *, base_ref: str | None = None) -> bool:
    """Return True when the merge base ref is an ancestor of HEAD."""
    base_ref = base_ref or _worktree_merge_base_ref(worktree)
    if base_ref is None:
        return False
    rc, _, _ = _git_output(["merge-base", "--is-ancestor", base_ref, "HEAD"], worktree, timeout=15)
    return rc == 0


def _parse_numstat(value: str) -> int:
    """Parse git numstat integers, treating binary markers as zero."""
    try:
        return int(value)
    except ValueError:
        return 0


def _worktree_dirty_files(worktree: Path) -> list[GitFileChange] | None:
    """Return ``git status --porcelain`` paths for uncommitted worktree files."""
    rc, out, _ = _git_output(["status", "--porcelain"], worktree, timeout=15)
    if rc != 0:
        return None

    files: list[GitFileChange] = []
    for line in out.splitlines():
        if not line:
            continue
        status = line[:2].strip() or line[:2]
        path = line[3:].strip() if len(line) > 3 else ""
        if path:
            files.append(GitFileChange(status=status, path=path))
    return files


def _worktree_dirty_numstats(worktree: Path) -> dict[str, tuple[int, int]]:
    """Return numstat details for dirty tracked files relative to ``HEAD``."""
    rc, out, _ = _git_output(
        ["diff", "--numstat", "--find-renames", "HEAD"],
        worktree,
        timeout=30,
    )
    if rc != 0:
        return {}

    numstats: dict[str, tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path = parts[-1].strip()
        if not path:
            continue
        numstats[path] = (_parse_numstat(parts[0]), _parse_numstat(parts[1]))
    return numstats


def _worktree_dirty_patch(worktree: Path) -> str | None:
    """Return a unified diff for dirty tracked files relative to ``HEAD``."""
    rc, out, _ = _git_output(
        ["diff", "--patch", "--find-renames", "HEAD"],
        worktree,
        timeout=30,
    )
    if rc != 0:
        return None
    return out.strip()


def _worktree_commit_shas(worktree: Path, *, base_ref: str | None = None) -> list[str]:
    """List commit SHAs reachable from HEAD but not from the merge base ref."""
    base_ref = base_ref or _worktree_merge_base_ref(worktree)
    if base_ref is None:
        return []
    rc, out, _ = _git_output(["rev-list", "--reverse", f"{base_ref}..HEAD"], worktree, timeout=30)
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _target_branch_contains_commit(repo: Path, branch: str, sha: str) -> bool:
    """Return True when ``sha`` is already reachable from ``repo``'s ``branch``."""
    rc, _, _ = _git_output(
        ["merge-base", "--is-ancestor", sha, f"refs/heads/{branch}"],
        repo,
        timeout=15,
    )
    return rc == 0


def _dashboard_pending_commit_shas(
    worktree: Path,
    repo_name: str,
    *,
    base_ref: str | None = None,
) -> list[str]:
    """Return worktree ahead SHAs that are not already merged into the target repo."""
    pending = _worktree_commit_shas(worktree, base_ref=base_ref)
    if repo_name != "autonomy":
        return pending

    target_branch, _target_head = _autonomy_target_branch_and_head()
    if target_branch is None:
        return pending

    return [
        sha for sha in pending
        if not _target_branch_contains_commit(REPO_ROOT, target_branch, sha)
    ]


def _worktree_clone_stale(repo_name: str, clone: Path | None) -> bool:
    """Return True when the managed clone lags the host integration branch."""
    if repo_name != "autonomy" or clone is None:
        return False

    target_branch, target_head = _autonomy_target_branch_and_head()
    if target_branch is None or target_head is None:
        return False

    clone_head = _repo_branch_head(clone, target_branch)
    if clone_head is None:
        return True
    return clone_head != target_head


def _commit_file_changes(worktree: Path, sha: str) -> list[GitFileChange]:
    """Return file-level status and numstat details for one commit."""
    numstats: dict[str, tuple[int, int]] = {}
    rc, out, _ = _git_output(
        ["show", "--numstat", "--format=", "--find-renames", sha],
        worktree,
        timeout=30,
    )
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            path = parts[-1].strip()
            if not path:
                continue
            numstats[path] = (_parse_numstat(parts[0]), _parse_numstat(parts[1]))

    rc, out, _ = _git_output(
        ["show", "--name-status", "--format=", "--find-renames", sha],
        worktree,
        timeout=30,
    )
    if rc != 0:
        return [
            GitFileChange(status="?", path=path, additions=adds, deletions=dels)
            for path, (adds, dels) in sorted(numstats.items())
        ]

    files: list[GitFileChange] = []
    seen: set[str] = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        raw_status = parts[0].strip()
        path = parts[-1].strip()
        if not raw_status or not path:
            continue
        status = raw_status[0]
        additions, deletions = numstats.get(path, (0, 0))
        files.append(GitFileChange(
            status=status,
            path=path,
            additions=additions,
            deletions=deletions,
        ))
        seen.add(path)

    for path, (additions, deletions) in sorted(numstats.items()):
        if path not in seen:
            files.append(GitFileChange(
                status="?",
                path=path,
                additions=additions,
                deletions=deletions,
            ))

    return files


def _read_worktree_commit(
    worktree: Path,
    sha: str,
    *,
    include_patch: bool = False,
) -> WorktreeCommit | None:
    """Read one commit from a worktree branch."""
    fmt = "%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1f%b"
    rc, out, _ = _git_output(
        ["show", "-s", f"--format={fmt}", "--date=format:%Y-%m-%d %H:%M", sha],
        worktree,
        timeout=15,
    )
    if rc != 0:
        return None
    parts = out.rstrip("\n").split("\x1f", 5)
    if len(parts) != 6:
        return None

    patch = None
    if include_patch:
        rc, patch_out, _ = _git_output(
            ["show", "--format=", "--patch", "--find-renames", sha],
            worktree,
            timeout=30,
        )
        if rc == 0:
            patch = patch_out.strip()

    return WorktreeCommit(
        sha=parts[0].strip(),
        short_sha=parts[1].strip(),
        author=parts[2].strip(),
        date=parts[3].strip(),
        subject=parts[4].strip(),
        body=parts[5].strip(),
        files=_commit_file_changes(worktree, parts[0].strip()),
        patch=patch,
    )


def _worktree_commits(
    worktree: Path,
    repo_name: str,
    *,
    base_ref: str | None = None,
) -> list[WorktreeCommit]:
    """Return dashboard-pending commit details for one worktree."""
    commits: list[WorktreeCommit] = []
    for sha in _dashboard_pending_commit_shas(worktree, repo_name, base_ref=base_ref):
        commit = _read_worktree_commit(worktree, sha)
        if commit is not None:
            commits.append(commit)
    return commits


def _resolve_worktree_commit(worktree: Path, sha: str) -> str:
    """Resolve a user-supplied SHA/prefix to a full commit SHA in ``worktree``."""
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise WorkspaceError(f"invalid commit SHA: {sha!r}")
    rc, out, err = _git_output(["rev-parse", "--verify", f"{sha}^{{commit}}"], worktree, timeout=15)
    if rc != 0:
        raise WorkspaceError(f"commit not found in worktree: {sha} ({err.strip()})")
    return out.strip()


def _live_session_names() -> set[str]:
    """Return live dashboard session names.

    Imported lazily so workspace-manager tests can run without importing the
    dashboard stack unless the scanner is actually used.
    """
    try:
        from tools.dashboard.dao.dashboard_db import get_live_sessions
    except Exception:
        return set()
    try:
        return {str(row["tmux_name"]) for row in get_live_sessions()}
    except Exception:
        logger.exception("workspace scan: failed to enumerate live sessions")
        return set()


def _session_is_live(session_name: str) -> bool:
    """Return True when the dashboard currently marks ``session_name`` live."""
    return session_name in _live_session_names()


def scan_all_worktrees(
    *,
    worktrees_dir: Path = WORKTREES_DIR,
    live_session_names: Iterable[str] | None = None,
) -> list[WorktreeState]:
    """Scan ``data/worktrees`` and return one state row per session/repo worktree."""
    if not worktrees_dir.exists():
        return []

    live = set(live_session_names) if live_session_names is not None else _live_session_names()
    out: list[WorktreeState] = []

    try:
        session_dirs = sorted(worktrees_dir.iterdir())
    except OSError:
        logger.exception("workspace scan: failed to enumerate %s", worktrees_dir)
        return []

    for session_dir in session_dirs:
        if not session_dir.is_dir():
            continue
        try:
            repo_dirs = sorted(session_dir.iterdir())
        except OSError:
            logger.exception("workspace scan: failed to enumerate %s", session_dir)
            continue
        for repo_dir in repo_dirs:
            if not repo_dir.is_dir():
                continue
            clone = _find_managed_clone_for_worktree(repo_dir)
            branch = _worktree_branch_name(repo_dir)
            base_ref = _worktree_dashboard_base_ref(repo_dir, repo_dir.name)
            clone_stale = _worktree_clone_stale(repo_dir.name, clone)
            dirty_files_or_none = _worktree_dirty_files(repo_dir)
            dirty_files = dirty_files_or_none or []
            # Preserve the previous safety behavior: if git status fails,
            # treat the worktree as dirty even though paths are unavailable.
            is_dirty = True if dirty_files_or_none is None else bool(dirty_files)
            commits = _worktree_commits(repo_dir, repo_dir.name, base_ref=base_ref)
            commits_ahead = _worktree_commits_ahead(repo_dir, base_ref=base_ref)
            ff_eligible = (
                branch is not None
                and bool(commits)
                and not clone_stale
                and not is_dirty
                and _worktree_ff_only_safe(repo_dir, base_ref=base_ref)
            )
            out.append(WorktreeState(
                session_name=session_dir.name,
                repo_name=repo_dir.name,
                worktree_path=repo_dir,
                managed_clone=clone,
                branch=branch,
                commits_ahead=commits_ahead,
                is_dirty=is_dirty,
                ff_eligible=ff_eligible,
                clone_stale=clone_stale,
                session_live=session_dir.name in live,
                commits=commits,
                dirty_files=dirty_files,
            ))

    return out


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


def cleanup_session_worktree(
    session_name: str,
    repo_name: str,
    *,
    force: bool = False,
    worktrees_dir: Path = WORKTREES_DIR,
) -> CleanupResult:
    """Remove one repo worktree for ``session_name`` while preserving others."""
    result = CleanupResult()
    session_dir = worktrees_dir / session_name
    entry = session_dir / repo_name
    if not entry.exists():
        return result
    if not entry.is_dir():
        result.errors.append((str(entry), "not a directory"))
        return result
    if session_name in _live_session_names():
        raise WorkspaceError(f"cannot discard live worktree for session {session_name}")

    branch = f"{SESSION_BRANCH_PREFIX}{session_name}"
    clone = _find_managed_clone_for_worktree(entry)

    if not force:
        reasons: list[str] = []
        if _worktree_has_uncommitted_changes(entry):
            reasons.append("uncommitted changes")
        elif _worktree_has_unpushed_commits(entry):
            reasons.append("unpushed commits")
        if reasons:
            result.preserved.append((str(entry), ", ".join(reasons)))
            logger.warning(
                "workspace cleanup: preserving %s (%s)",
                entry, ", ".join(reasons),
            )
            return result

    if clone is None:
        try:
            shutil.rmtree(entry)
            result.removed.append(str(entry))
        except OSError as e:
            result.errors.append((str(entry), f"rmtree: {e}"))
            return result
    else:
        ok, err = _worktree_remove(clone, entry)
        if not ok:
            if entry.exists():
                try:
                    shutil.rmtree(entry)
                except OSError as e:
                    result.errors.append((str(entry), f"remove failed: {err}; rmtree: {e}"))
                    return result
            logger.info(
                "workspace cleanup: 'worktree remove' failed for %s (%s); "
                "fell back to rmtree",
                entry, err,
            )

        result.removed.append(str(entry))
        _delete_branch(clone, branch)
        _worktree_prune(clone)

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


def merge_session_worktree(
    session_name: str,
    repo_name: str,
    *,
    worktrees_dir: Path = WORKTREES_DIR,
) -> dict[str, str]:
    """Fast-forward a local checkout from a session worktree's branch."""
    worktree = worktrees_dir / session_name / repo_name
    if not worktree.exists() or not worktree.is_dir():
        raise WorkspaceError(f"worktree not found: {worktree}")

    clone = _find_managed_clone_for_worktree(worktree)
    if clone is None:
        raise WorkspaceError(f"managed clone not found for worktree: {worktree}")

    branch = _worktree_branch_name(worktree)
    if branch is None:
        raise WorkspaceError(f"worktree is detached or unreadable: {worktree}")

    is_dirty = _worktree_has_uncommitted_changes(worktree)
    base_ref = _worktree_dashboard_base_ref(worktree, repo_name)
    commits_ahead = _worktree_commits_ahead(worktree, base_ref=base_ref)
    ff_eligible = commits_ahead > 0 and not is_dirty and _worktree_ff_only_safe(worktree, base_ref=base_ref)
    if not ff_eligible:
        raise WorkspaceError(
            f"worktree {session_name}/{repo_name} is not ff-eligible "
            f"(ahead={commits_ahead}, dirty={is_dirty}, branch={branch!r})"
        )

    # Only the autonomy repo has a known local checkout in this process: the
    # dashboard's own repository. Cross-repo merge targets need explicit
    # workspace metadata before they can be made safe.
    if repo_name != "autonomy":
        raise WorkspaceError(
            f"merge target unsupported for repo {repo_name!r}; "
            "only 'autonomy' can be merged from the dashboard today"
        )

    target_branch, _target_head = _autonomy_target_branch_and_head()
    if target_branch is None:
        raise WorkspaceError("could not determine autonomy integration branch")

    target_repo = REPO_ROOT
    _run_git(["fetch", str(clone), branch], cwd=target_repo)
    target_sha = _run_git(["rev-parse", "FETCH_HEAD"], cwd=target_repo).strip()
    rc, current_head, _ = _git_output(
        ["rev-parse", "--verify", f"refs/heads/{target_branch}"],
        target_repo,
        timeout=15,
    )
    if rc != 0:
        raise WorkspaceError(f"target branch not found: {target_branch}")
    current_head = current_head.strip()

    rc, _, _ = _git_output(["merge-base", "--is-ancestor", current_head, target_sha], target_repo, timeout=15)
    if rc != 0:
        raise WorkspaceError(
            f"selected branch tip is not fast-forward eligible from {target_branch}"
        )

    rc, head_branch, _ = _git_output(["symbolic-ref", "--quiet", "--short", "HEAD"], target_repo, timeout=15)
    if rc == 0 and head_branch.strip() == target_branch:
        _run_git(["merge", "--ff-only", target_sha], cwd=target_repo)
    else:
        _run_git(
            ["update-ref", f"refs/heads/{target_branch}", target_sha, current_head],
            cwd=target_repo,
        )

    commit = _run_git(
        ["rev-parse", "--verify", f"refs/heads/{target_branch}"],
        cwd=target_repo,
    ).strip()
    _sync_managed_clone_branch_ref(clone, target_repo, target_branch)
    message = _run_git(["log", "-1", "--pretty=%s", commit], cwd=target_repo).strip()
    return {
        "commit": commit,
        "message": message,
        "target_repo": str(target_repo),
        "target_branch": target_branch,
    }


def _session_worktree_path(
    session_name: str,
    repo_name: str,
    *,
    worktrees_dir: Path = WORKTREES_DIR,
) -> Path:
    worktree = worktrees_dir / session_name / repo_name
    if not worktree.exists() or not worktree.is_dir():
        raise WorkspaceError(f"worktree not found: {worktree}")
    return worktree


def _session_worktree_context(
    session_name: str,
    repo_name: str,
    *,
    worktrees_dir: Path = WORKTREES_DIR,
) -> tuple[Path, Path, str]:
    """Resolve a session/repo pair to worktree, managed clone, and branch."""
    worktree = _session_worktree_path(
        session_name,
        repo_name,
        worktrees_dir=worktrees_dir,
    )

    clone = _find_managed_clone_for_worktree(worktree)
    if clone is None:
        raise WorkspaceError(f"managed clone not found for worktree: {worktree}")

    branch = _worktree_branch_name(worktree)
    if branch is None:
        raise WorkspaceError(f"worktree is detached or unreadable: {worktree}")

    return worktree, clone, branch


def _rebase_required_info(
    session_name: str,
    target_repo: Path,
    target_branch: str,
    commit_sha: str,
) -> dict[str, str | int | bool]:
    """Return structured rebase metadata for ``commit_sha`` against ``target_branch``."""
    rc, out, err = _git_output(
        ["merge-base", f"refs/heads/{target_branch}", commit_sha],
        target_repo,
        timeout=15,
    )
    if rc != 0:
        raise WorkspaceError(
            f"could not determine fork point for {commit_sha[:7]} against {target_branch}: {err.strip()}"
        )
    fork_sha = out.strip()
    rc, out, err = _git_output(
        ["rev-list", "--count", f"{fork_sha}..refs/heads/{target_branch}"],
        target_repo,
        timeout=15,
    )
    if rc != 0:
        raise WorkspaceError(
            f"could not determine commits behind for {commit_sha[:7]} against {target_branch}: {err.strip()}"
        )
    try:
        commits_behind = int(out.strip() or "0")
    except ValueError as exc:
        raise WorkspaceError(
            f"could not parse commits-behind count for {commit_sha[:7]} against {target_branch}"
        ) from exc
    return {
        "target_branch": target_branch,
        "commits_behind": commits_behind,
        "fork_sha": fork_sha,
        "session_live": _session_is_live(session_name),
    }


def get_session_worktree_rebase_info(
    session_name: str,
    repo_name: str,
    *,
    worktrees_dir: Path = WORKTREES_DIR,
    sync_managed_clone_target: bool = False,
) -> dict[str, str | int | bool]:
    """Return rebase guidance for the next pending dashboard commit."""
    worktree, clone, branch = _session_worktree_context(
        session_name,
        repo_name,
        worktrees_dir=worktrees_dir,
    )

    if repo_name != "autonomy":
        raise WorkspaceError(
            f"merge target unsupported for repo {repo_name!r}; "
            "only 'autonomy' can be merged from the dashboard today"
        )

    target_branch, _target_head = _autonomy_target_branch_and_head()
    if target_branch is None:
        raise WorkspaceError("could not determine autonomy integration branch")

    if sync_managed_clone_target:
        _sync_managed_clone_branch_ref(clone, REPO_ROOT, target_branch)

    # Ensure the target repo can resolve the selected commit SHA when we
    # compute fork-point / behind counts for request-rebase and merge errors.
    _run_git(["fetch", str(clone), branch], cwd=REPO_ROOT)

    base_ref = _worktree_dashboard_base_ref(worktree, repo_name)
    pending = _dashboard_pending_commit_shas(worktree, repo_name, base_ref=base_ref)
    if not pending:
        raise WorkspaceError("no pending commits for this worktree")

    info = _rebase_required_info(
        session_name,
        REPO_ROOT,
        target_branch,
        pending[0],
    )
    info["commit"] = pending[0]
    return info


def sync_session_worktree_base(
    session_name: str,
    repo_name: str,
    *,
    worktrees_dir: Path = WORKTREES_DIR,
) -> dict[str, str]:
    """Sync the managed clone integration branch from the host checkout."""
    _worktree, clone, _branch = _session_worktree_context(
        session_name,
        repo_name,
        worktrees_dir=worktrees_dir,
    )

    if repo_name != "autonomy":
        raise WorkspaceError(
            f"merge target unsupported for repo {repo_name!r}; "
            "only 'autonomy' can be merged from the dashboard today"
        )

    target_branch, _target_head = _autonomy_target_branch_and_head()
    if target_branch is None:
        raise WorkspaceError("could not determine autonomy integration branch")

    _sync_managed_clone_branch_ref(clone, REPO_ROOT, target_branch)
    return {
        "target_branch": target_branch,
        "managed_clone": str(clone),
    }


def get_session_worktree_commit_detail(
    session_name: str,
    repo_name: str,
    sha: str,
    *,
    worktrees_dir: Path = WORKTREES_DIR,
) -> WorktreeCommit:
    """Return one ahead commit, including its patch, for API review."""
    worktree, _clone, _branch = _session_worktree_context(
        session_name,
        repo_name,
        worktrees_dir=worktrees_dir,
    )
    base_ref = _worktree_dashboard_base_ref(worktree, repo_name)
    resolved = _resolve_worktree_commit(worktree, sha)
    pending = _dashboard_pending_commit_shas(worktree, repo_name, base_ref=base_ref)
    if resolved not in pending:
        raise WorkspaceError(f"commit is not in worktree ahead range: {sha}")
    commit = _read_worktree_commit(worktree, resolved, include_patch=True)
    if commit is None:
        raise WorkspaceError(f"commit could not be read: {sha}")
    return commit


def get_session_worktree_dirty_detail(
    session_name: str,
    repo_name: str,
    *,
    worktrees_dir: Path = WORKTREES_DIR,
) -> WorktreeDirtyDetail:
    """Return dirty file metadata and unified diff for one worktree."""
    worktree = _session_worktree_path(
        session_name,
        repo_name,
        worktrees_dir=worktrees_dir,
    )
    dirty_files = _worktree_dirty_files(worktree)
    if dirty_files is None:
        raise WorkspaceError(f"could not read dirty file state for worktree: {worktree}")

    numstats = _worktree_dirty_numstats(worktree)
    files = [
        GitFileChange(
            status=file.status,
            path=file.path,
            additions=numstats.get(file.path, (0, 0))[0],
            deletions=numstats.get(file.path, (0, 0))[1],
        )
        for file in dirty_files
    ]
    return WorktreeDirtyDetail(
        files=files,
        patch=_worktree_dirty_patch(worktree),
    )

def merge_session_worktree_commit(
    session_name: str,
    repo_name: str,
    sha: str,
    *,
    worktrees_dir: Path = WORKTREES_DIR,
) -> dict[str, str]:
    """Fast-forward the local checkout to a selected session commit."""
    worktree, clone, branch = _session_worktree_context(
        session_name,
        repo_name,
        worktrees_dir=worktrees_dir,
    )

    if repo_name != "autonomy":
        raise WorkspaceError(
            f"merge target unsupported for repo {repo_name!r}; "
            "only 'autonomy' can be merged from the dashboard today"
        )

    base_ref = _worktree_dashboard_base_ref(worktree, repo_name)
    resolved = _resolve_worktree_commit(worktree, sha)
    pending = _dashboard_pending_commit_shas(worktree, repo_name, base_ref=base_ref)
    if resolved not in pending:
        raise WorkspaceError(f"commit is not in worktree ahead range: {sha}")
    if not pending or pending[0] != resolved:
        raise WorkspaceError(
            f"selected commit {resolved[:7]} is not the next pending commit for this worktree"
        )

    target_branch, _target_head = _autonomy_target_branch_and_head()
    if target_branch is None:
        raise WorkspaceError("could not determine autonomy integration branch")
    if _worktree_clone_stale(repo_name, clone):
        raise WorkspaceError("managed clone base is stale; sync worktree to latest before merge")

    target_repo = REPO_ROOT
    _run_git(["fetch", str(clone), branch], cwd=target_repo)

    rc, current_head, _ = _git_output(
        ["rev-parse", "--verify", f"refs/heads/{target_branch}"],
        target_repo,
        timeout=15,
    )
    if rc != 0:
        raise WorkspaceError(f"target branch not found: {target_branch}")
    current_head = current_head.strip()

    rc, _, _ = _git_output(["merge-base", "--is-ancestor", current_head, resolved], target_repo, timeout=15)
    if rc != 0:
        info = _rebase_required_info(session_name, target_repo, target_branch, resolved)
        raise RebaseRequiredError(
            target_branch=target_branch,
            commits_behind=int(info["commits_behind"]),
            fork_sha=str(info["fork_sha"]),
            session_live=bool(info["session_live"]),
        )

    rc, head_branch, _ = _git_output(["symbolic-ref", "--quiet", "--short", "HEAD"], target_repo, timeout=15)
    if rc == 0 and head_branch.strip() == target_branch:
        _run_git(["merge", "--ff-only", resolved], cwd=target_repo)
    else:
        _run_git(
            ["update-ref", f"refs/heads/{target_branch}", resolved, current_head],
            cwd=target_repo,
        )

    commit = _run_git(
        ["rev-parse", "--verify", f"refs/heads/{target_branch}"],
        cwd=target_repo,
    ).strip()
    _sync_managed_clone_branch_ref(clone, target_repo, target_branch)
    message = _run_git(["log", "-1", "--pretty=%s", commit], cwd=target_repo).strip()
    return {
        "commit": commit,
        "message": message,
        "target_repo": str(target_repo),
        "target_branch": target_branch,
    }
