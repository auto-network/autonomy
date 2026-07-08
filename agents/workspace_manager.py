"""Per-session workspace preparation — managed clones and worktrees.

Given a WorkspaceV1 and a session name, this module:

1. Ensures a managed clone of each repo URL exists under ``data/repos/``.
2. Runs ``git fetch origin --prune`` on every clone.
3. For writable repos, creates a per-session worktree under
   ``data/worktrees/{session_name}/`` on a fresh ``session/{session_name}``
   branch based on the managed clone's current integration base
   (local ``main``/``master`` when that contains newer unpushed work,
   otherwise the freshest remote-tracking default branch).
4. Returns a mount spec dict for ``agents.session_launcher.launch_session``.

Mount layout for a writable repo:
    - worktree → container mount path (rw)
    - managed clone → its own absolute host path (rw) so the worktree's
      ``.git`` file (which uses absolute paths) resolves inside the container.
      The clone must be rw because ``git add``/``commit`` in the worktree
      writes into ``<clone>/.git/worktrees/<name>/`` (index, HEAD, refs) and
      into the clone's shared object store.

Read-only repos are checked out to that same current integration base in the
managed clone itself and mounted directly at the container mount path.

SSH credentials for ``git clone``/``git fetch`` come from the host user's
environment (SSH agent or ~/.ssh keys) — the dashboard server runs on the
host, not in a container.

Design refs:
    graph://e9448254-18f  Pluggable project-specific container sessions
    graph://eabec73c-baa  Workspaces & Orgs signpost
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from agents.git_status import has_working_tree_changes
from agents.workspace_settings import (
    RepoMount,
    WorkspaceV1,
    WorkspaceMountInvalidError,
    WorkspaceMountMissingError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
REPOS_DIR = DATA_DIR / "repos"
WORKTREES_DIR = DATA_DIR / "worktrees"

logger = logging.getLogger(__name__)


def _refuse_pytest_against_real_worktrees(worktrees_dir: Path, op: str) -> None:
    if worktrees_dir == WORKTREES_DIR and "pytest" in sys.modules:
        raise RuntimeError(
            f"refusing {op} against production WORKTREES_DIR while pytest is loaded"
        )


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


def _is_local_url(url: str) -> bool:
    """True when ``url`` is an absolute host filesystem path, not a git URL.

    Local-first repos (e.g. a ``git svn`` mirror with no git remote) are
    configured with ``url`` set to the absolute host checkout path. ``git
    clone``/``fetch`` work against a local path directly, so no network
    remote is required — only the URL *parsing* and clone-path derivation
    need to special-case it. SSH scp-form (``user@host:path``) and
    ``scheme://`` URLs never start with ``/``.
    """
    return url.startswith("/")


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
    if _is_local_url(url):
        # Local checkout (no host/path URL to parse). Store the managed clone
        # under a deterministic ``local/`` subtree mirroring the host path so
        # it is unique and self-describing.
        return repos_dir / "local" / f"{url.strip('/')}.git"
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


def ensure_managed_clone(
    url: str,
    *,
    repos_dir: Path = REPOS_DIR,
    git_timeout: int = 600,
) -> Path:
    """Clone ``url`` under ``repos_dir`` if missing, otherwise fetch + prune.

    Returns the path to the managed clone. Subsequent calls for the same URL
    are idempotent (just a fetch).
    """
    clone_path = managed_clone_path(url, repos_dir=repos_dir)
    if clone_path.exists():
        logger.info("workspace: fetching %s", clone_path)
        _run_git(["fetch", "origin", "--prune"], cwd=clone_path, timeout=git_timeout)
        # Refresh the local integration branch when it is merely stale behind
        # origin, but preserve any local-only commits operators may have
        # staged onto the managed clone as the current base.
        default = _repo_default_branch(clone_path)
        if default:
            _refresh_local_branch_from_remote(clone_path, default, timeout=min(git_timeout, 15))
    else:
        logger.info("workspace: cloning %s → %s", url, clone_path)
        clone_path.parent.mkdir(parents=True, exist_ok=True)
        clone_args = ["clone"]
        if _is_local_url(url):
            # Local source on the same filesystem: force a self-contained copy
            # (no object hardlinks into the host checkout) so the managed clone
            # is safe to mount into containers independently of the checkout.
            clone_args.append("--no-hardlinks")
        clone_args += [url, str(clone_path)]
        _run_git(clone_args, timeout=git_timeout)
    return clone_path


def _worktree_basename(url: str) -> str:
    if _is_local_url(url):
        return Path(url.rstrip("/")).name
    _host, path = parse_repo_url(url)
    return path.rsplit("/", 1)[-1]


def _worktree_metadata_name(worktree_dir: Path) -> str:
    """Return the bare clone's metadata-dir name for this worktree.

    The worktree's ``.git`` file is a pointer of the form ``gitdir: <path>``
    where ``<path>`` ends in ``<clone>/.git/worktrees/<name>``. ``<name>`` is
    usually the worktree path's basename but git appends a numeric suffix on
    collisions (e.g. ``enterprise_ng14``), so we read it back from disk
    rather than recomputing it.
    """
    git_file = (worktree_dir / ".git").read_text().strip()
    if not git_file.startswith("gitdir:"):
        raise RuntimeError(
            f"worktree {worktree_dir} has unexpected .git contents: {git_file[:120]!r}"
        )
    gitdir = git_file.split(":", 1)[1].strip()
    return Path(gitdir).name


def _refresh_existing_worktree(
    managed_clone: Path,
    worktree_dir: Path,
    branch: str,
    *,
    git_timeout: int = 600,
) -> None:
    """Refresh a reused worktree to the current integration base when safe.

    Fresh workspace launches may reuse an old per-session worktree directory if a
    prior attempt with the same tmux name already created it. In that case we
    want a current checkout, not a stale branch from some earlier origin state.

    Safety rule:
    - only refresh when the worktree is still on the expected session branch
    - only refresh when there are no local session commits ahead of the
      managed clone's current integration base

    Uncommitted changes/untracked files are discarded in this path on purpose:
    they are stale byproducts from the earlier failed launch, not resume state.
    Resume flows call ``prepare_session_mounts(..., refresh_existing_worktree=False)``
    and therefore bypass this reset entirely.
    """
    current_branch = _worktree_branch_name(worktree_dir)
    if current_branch != branch:
        logger.info(
            "workspace: preserving existing worktree %s (branch=%s expected=%s)",
            worktree_dir, current_branch, branch,
        )
        return
    if _worktree_has_commits_ahead_of_base(worktree_dir):
        logger.info(
            "workspace: preserving existing worktree %s (local session commits ahead of base)",
            worktree_dir,
        )
        return
    base_ref = _repo_integration_base_ref(managed_clone)
    logger.info(
        "workspace: refreshing existing worktree %s to %s",
        worktree_dir,
        base_ref,
    )
    _run_git(["reset", "--hard", base_ref], cwd=worktree_dir, timeout=git_timeout)
    _run_git(["clean", "-fd"], cwd=worktree_dir, timeout=git_timeout)


def create_worktree(
    managed_clone: Path,
    worktree_dir: Path,
    branch: str,
    *,
    refresh_existing: bool = False,
    git_timeout: int = 600,
) -> Path:
    """Create a new worktree at ``worktree_dir`` on ``branch`` from the
    managed clone's current integration base.

    If the worktree already exists it is reused. Callers can request a safe
    refresh of stale launch leftovers via ``refresh_existing=True``.
    """
    if worktree_dir.exists():
        if refresh_existing:
            _refresh_existing_worktree(
                managed_clone,
                worktree_dir,
                branch,
                git_timeout=git_timeout,
            )
        return worktree_dir
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    # If the session branch already exists in the managed clone — typically
    # because a prior cleanup deleted its worktree but couldn't reach the
    # _delete_branch step (rmtree EACCES on root-owned __pycache__) — attach
    # a fresh worktree to the existing branch instead of failing with
    # "branch already exists". The branch may carry committed work the
    # operator wants to resume on top of; force-recreating would lose it.
    rc, _, _ = _git_output(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=managed_clone,
    )
    if rc == 0:
        _run_git(
            ["worktree", "add", str(worktree_dir), branch],
            cwd=managed_clone,
            timeout=git_timeout,
        )
    else:
        base_ref = _repo_integration_base_ref(managed_clone)
        _run_git(
            ["worktree", "add", "-b", branch, str(worktree_dir), base_ref],
            cwd=managed_clone,
            timeout=git_timeout,
        )
    return worktree_dir


def _update_readonly_clone(clone: Path, *, git_timeout: int = 600) -> None:
    """Fast-forward the managed clone's working tree to the current base ref.

    Read-only repos are mounted directly from the managed clone, so the
    clone's own checkout must be current. We use ``checkout --detach`` so
    the clone stays on a detached HEAD and never conflicts with worktrees.
    """
    _run_git(
        ["checkout", "--detach", _repo_integration_base_ref(clone)],
        cwd=clone,
        timeout=git_timeout,
    )


def _repo_identity(url: str) -> tuple[str, str] | str:
    """Return a comparable canonical identity for a git repo location.

    URL-shaped values resolve to the ``(host, path)`` pair from
    :func:`parse_repo_url`; file-system paths fall back to a resolved
    absolute string so on-disk repos compare cleanly across symlinks and
    relative forms. Used to verify a host checkout's ``origin`` matches a
    workspace repo URL when ``base_source`` is set.
    """
    try:
        return parse_repo_url(url)
    except WorkspaceError:
        return str(Path(url).resolve())


def _sync_managed_clone_from_base_source(
    repo: RepoMount,
    clone: Path,
    *,
    git_timeout: int = 600,
) -> None:
    """Sync ``clone``'s integration branch from ``repo.base_source``.

    Validates that the host path exists, is a git checkout, and its
    ``origin`` URL identity matches ``repo.url``. Resolves the checkout's
    default integration branch and advances the managed clone's local
    branch ref to that tip via :func:`_sync_managed_clone_branch_ref`.

    Raises :class:`WorkspaceError` (loudly, with no fallback to ``origin``)
    when the path is missing, not a git checkout, has no matching
    ``origin``, or has no resolvable default branch.
    """
    base_source = repo.base_source
    if base_source is None:
        return
    if _is_local_url(repo.url) and _repo_identity(base_source) == _repo_identity(repo.url):
        # ``url`` IS the host checkout — a local-first repo with no separate git
        # remote (e.g. a git-svn mirror). ``ensure_managed_clone`` already cloned/
        # fetched the managed clone directly from it, so it is already in sync, and
        # there is no ``origin`` on the checkout to reconcile against. (This is
        # distinct from the base_source feature proper, where ``url`` is a real
        # remote and ``base_source`` is a *different* local checkout of it.)
        logger.info(
            "workspace: repo %r is its own local source — managed clone tracks it "
            "directly; skipping base_source reconciliation",
            repo.url,
        )
        return
    if not base_source.startswith("/"):
        raise WorkspaceError(
            f"workspace: repo {repo.url!r} base_source must be an absolute "
            f"path, got {base_source!r}"
        )
    host_path = Path(base_source)
    if not host_path.exists():
        raise WorkspaceError(
            f"workspace: base_source path does not exist for repo "
            f"{repo.url!r}: {base_source}"
        )
    rc, _, err = _git_output(
        ["rev-parse", "--git-dir"], host_path, timeout=15,
    )
    if rc != 0:
        raise WorkspaceError(
            f"workspace: base_source is not a git checkout for repo "
            f"{repo.url!r}: {base_source} ({err.strip()})"
        )
    rc, origin_url, err = _git_output(
        ["config", "--get", "remote.origin.url"], host_path, timeout=15,
    )
    if rc != 0 or not origin_url.strip():
        raise WorkspaceError(
            f"workspace: base_source has no origin remote for repo "
            f"{repo.url!r}: {base_source}"
        )
    origin = origin_url.strip()
    if _repo_identity(origin) != _repo_identity(repo.url):
        raise WorkspaceError(
            f"workspace: base_source {base_source} origin {origin!r} does "
            f"not match repo URL {repo.url!r}"
        )
    branch = _repo_default_branch(host_path)
    if branch is None:
        raise WorkspaceError(
            f"workspace: could not resolve default branch for base_source "
            f"of repo {repo.url!r}: {base_source}"
        )
    logger.info(
        "workspace: syncing managed clone %s from base_source %s (branch %s)",
        clone, base_source, branch,
    )
    _sync_managed_clone_branch_ref(clone, host_path, branch, timeout=git_timeout)


COMMIT_SIGN_SHIM = os.environ.get("COMMIT_SIGN_SHIM", "/usr/local/bin/commit-sign-shim")


def configure_commit_signing(
    worktree: Path,
    *,
    session_name: str,
    repo_slug: str,
    org: str | None = None,
    workspace_id: str | None = None,
    shim_path: str = COMMIT_SIGN_SHIM,
) -> bool:
    """Configure a worktree to sign commits via the operator when its policy says so.

    If the workspace's resolved commit policy requires a GPG signature, write the
    git config so an ordinary ``git commit`` routes through the signing shim (the
    operator reviews + signs in their browser; the agent never sees a key):
    ``commit.gpgSign true``, ``gpg.program`` -> the shim, the org signing identity
    for GitHub "Verified", and ``autonomy.sign.session``/``repo`` for the shim to
    read. Returns True if signing was configured. Idempotent and best-effort —
    never raises into worktree setup.
    """
    try:
        from tools.graph.commit_policy import resolve_commit_policy
        resolved = resolve_commit_policy(
            workspace_id=workspace_id, repo_slug=repo_slug, org=org,
        )
        payload = resolved.payload or {}
        if "gpg" not in str(payload.get("signature_requirement", "none")):
            return False
        cfg: list[tuple[str, str]] = [
            ("commit.gpgSign", "true"),
            ("gpg.program", shim_path),
            ("autonomy.sign.session", session_name),
            # Worktree dir name — the dashboard locates the worktree by it.
            ("autonomy.sign.repo", Path(worktree).name),
        ]
        # Org signing identity so GitHub shows "Verified" (committer email must
        # match a verified email on the key's account). Best-effort from policy.
        ident = payload.get("author_policy", {}).get("required_identity") or {}
        if isinstance(ident, dict):
            if ident.get("email"):
                cfg.append(("user.email", str(ident["email"])))
                cfg.append(("user.signingKey", str(ident["email"])))
            if ident.get("name"):
                cfg.append(("user.name", str(ident["name"])))
        for key, value in cfg:
            _run_git(["config", key, value], cwd=worktree)
        return True
    except Exception:
        logger.exception("configure_commit_signing failed for %s", worktree)
        return False


def prepare_session_mounts(
    workspace: WorkspaceV1,
    session_name: str,
    *,
    repos_dir: Path = REPOS_DIR,
    worktrees_dir: Path = WORKTREES_DIR,
    refresh_existing_worktree: bool = False,
    progress_callback: Callable[[int, int, str], None] | None = None,
    git_timeout: int = 600,
) -> dict[str, str]:
    """Prepare clones + worktrees for ``workspace`` and return launch_session mounts.

    The returned dict maps host paths to ``container_path[:mode]`` strings,
    suitable for ``launch_session(mounts=...)``.

    ``progress_callback`` (auto-ja51w): optional ``(repo_index, total_repos,
    repo_name) -> None`` called once per repo as each completes. Used by
    ``api_session_create`` to broadcast per-repo progress to the SSE registry
    while this function runs inside ``asyncio.to_thread``. Callback exceptions
    are swallowed — progress reporting must never break the actual mount prep.
    """
    mounts: dict[str, str] = {}
    total = len(workspace.repos)
    for idx, repo in enumerate(workspace.repos):
        clone = ensure_managed_clone(
            repo.url,
            repos_dir=repos_dir,
            git_timeout=git_timeout,
        )
        _sync_managed_clone_from_base_source(repo, clone, git_timeout=git_timeout)
        if repo.writable:
            worktree = worktrees_dir / session_name / _worktree_basename(repo.url)
            create_worktree(
                clone,
                worktree,
                f"session/{session_name}",
                refresh_existing=refresh_existing_worktree,
                git_timeout=git_timeout,
            )
            # If the workspace's commit policy requires a GPG signature, wire the
            # worktree to sign via the operator's browser (best-effort; a policy
            # that doesn't require signing writes nothing, and any failure is
            # swallowed so it never breaks mount prep).
            try:
                from agents.capabilities.github.service import derive_repo_slug
                _repo_slug = derive_repo_slug(repo.url)
            except Exception:
                _repo_slug = _worktree_basename(repo.url)
            configure_commit_signing(
                worktree,
                session_name=session_name,
                repo_slug=_repo_slug,
                org=getattr(workspace, "graph_project", None),
                workspace_id=getattr(workspace, "workspace_id", None)
                or getattr(workspace, "id", None),
            )
            mounts[str(worktree)] = repo.mount
            # Worktree's .git file points at an absolute host path inside the
            # managed clone — mount the clone at that same path (rw) so the
            # container can resolve it and so ``git add``/``commit`` can
            # write the worktree's per-worktree git state (index, refs) that
            # lives at ``<clone>/.git/worktrees/<name>/``.
            mounts[str(clone)] = str(clone)
            # Protect the bare clone's shared worktree metadata from cross-
            # container corruption (e.g. an in-container ``git worktree prune``
            # that decides every sibling session's host path is missing).
            # The parent ``.git/worktrees/`` is bind-mounted read-only on top
            # of the clone mount; the container's own session metadata subdir
            # is then bind-mounted read-write on top of that, so normal git
            # ops in this worktree (``add``/``commit`` writing index/HEAD/refs
            # into ``<clone>/.git/worktrees/<name>/``) still work while
            # ``prune``/``add``/``remove`` against siblings fails with EACCES.
            git_worktrees_dir = clone / ".git" / "worktrees"
            own_metadata_name = _worktree_metadata_name(worktree)
            mounts[str(git_worktrees_dir)] = f"{git_worktrees_dir}:ro"
            mounts[str(git_worktrees_dir / own_metadata_name)] = str(
                git_worktrees_dir / own_metadata_name
            )
        else:
            _update_readonly_clone(clone, git_timeout=git_timeout)
            mounts[str(clone)] = f"{repo.mount}:ro"
        if progress_callback is not None:
            try:
                progress_callback(idx + 1, total, _worktree_basename(repo.url))
            except Exception:
                logger.debug(
                    "prepare_session_mounts: progress_callback raised — swallowing",
                    exc_info=True,
                )
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
        # A mount host path may be a directory OR a single file — the
        # narrowest credential mount is one key file, not a directory (e.g.
        # blindhash-operations' encrypted decrypt key at
        # .../private-key-encrypted.pem). Docker bind-mounts both. The old
        # is_dir() check rejected legitimate single-file secret mounts and,
        # because WorkspaceMountInvalidError wasn't caught by the
        # session-create handler, crashed the request with an unhandled 500.
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
    rebase_required: bool
    session_live: bool
    cherry_pick_eligible: bool = False
    cherry_pick_commit: str | None = None
    commits: list[WorktreeCommit] = field(default_factory=list)
    dirty_files: list[GitFileChange] = field(default_factory=list)


@dataclass(frozen=True)
class WorktreeDirtyDetail:
    """Uncommitted file detail for a worktree review screen.

    ``stale`` flags the case where the requested base/head SHAs no
    longer exist locally (force-push to a rewritten branch, an
    operator-bound review whose head SHA was orphaned by a rebase,
    etc.). The dashboard renders a "stale, refresh required"
    indicator instead of crashing.
    """

    files: list[GitFileChange] = field(default_factory=list)
    patch: str | None = None
    stale: bool = False
    reason: str | None = None


# Process-lifetime count of ``_git_output`` invocations. Coarse and
# unscoped by design: callers that want a sweep-scoped count take a
# before/after snapshot via :func:`git_call_count` rather than us
# threading a counter object through every helper's signature (which
# would touch every call site in this module). Approximate under
# concurrent git usage from other paths (an operator action racing a
# sweep) — acceptable for attribution logging, not a hard metric.
_git_call_total = 0


def git_call_count() -> int:
    """Return the process-lifetime ``_git_output`` call count.

    Callers scope this to one operation by diffing two snapshots, e.g.
    ``before = git_call_count(); ...; calls = git_call_count() - before``.
    """
    return _git_call_total


def _git_output(args: list[str], cwd: Path, *, timeout: int = 15) -> tuple[int, str, str]:
    """Run git and return (rc, stdout, stderr); never raises on non-zero exit."""
    global _git_call_total
    _git_call_total += 1
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


def _force_rmtree(path: Path) -> None:
    """Remove a tree, escalating via a root-uid container when host rm
    can't unlink files written by container-root processes.

    Dispatched containers that run tooling as root (tox/pytest/pip)
    create on-disk artefacts — most often Python ``__pycache__/`` —
    owned by host UID 0 because there's no userns-remap. The host user
    running cleanup can't ``unlink`` those files, so ``shutil.rmtree``
    fails with EACCES partway through, leaving a half-deleted worktree
    that the next launch silently bind-mounts.

    The escalation path mounts ``path``'s parent into an ``alpine``
    container and runs ``rm -rf``. The docker daemon runs as host root,
    and the container's UID 0 is the same UID realm that wrote the
    files in the first place, so deletions that EACCES'd at the host
    level succeed. The mount is scoped to the parent so the rm target
    can only resolve to a single named child, not arbitrary host
    paths.

    Raises ``OSError`` if both paths fail or if the directory still
    exists after the docker fallback.
    """
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        logger.info(
            "workspace cleanup: host rmtree hit EACCES on %s; "
            "escalating via docker rm", path,
        )
    parent = path.parent
    name = path.name
    try:
        r = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{parent}:/wt",
             "alpine", "rm", "-rf", f"/wt/{name}"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise OSError(f"docker rmtree timed out for {path}") from None
    except FileNotFoundError as e:
        raise OSError(f"docker not on PATH for rmtree fallback: {e}") from None
    if r.returncode != 0 or path.exists():
        raise OSError(
            f"docker rmtree failed for {path}: rc={r.returncode} "
            f"stderr={r.stderr.strip()}"
        )
    logger.info(
        "workspace cleanup: docker rm succeeded after host EACCES on %s",
        path,
    )


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


def _refresh_local_branch_from_remote(
    repo: Path,
    branch: str,
    *,
    timeout: int = 15,
) -> None:
    """Fast-forward ``refs/heads/<branch>`` from origin when safe.

    Managed clones should normally track origin exactly. The only supported
    case where the local integration branch may intentionally diverge is when
    the dashboard explicitly synced it from another checkout (``base_source``
    or the operator-triggered sync-base path). Preserve only that explicit
    synced-source branch; otherwise rewrite local divergence back to origin so
    stale accidental clone commits cannot pin future launches.
    """
    local_ref = f"refs/heads/{branch}"
    remote_ref = f"refs/remotes/origin/{branch}"
    local_ok = _git_output(["rev-parse", "--verify", local_ref], repo, timeout=timeout)[0] == 0
    remote_ok = _git_output(["rev-parse", "--verify", remote_ref], repo, timeout=timeout)[0] == 0
    if not remote_ok:
        return
    if not local_ok:
        _git_output(["update-ref", local_ref, remote_ref], repo, timeout=timeout)
        return

    local_head = _git_output(["rev-parse", "--verify", local_ref], repo, timeout=timeout)[1].strip()
    remote_head = _git_output(["rev-parse", "--verify", remote_ref], repo, timeout=timeout)[1].strip()
    if not local_head or not remote_head:
        return
    sync_head = _managed_clone_synced_source_head(repo, branch)
    if local_head == remote_head:
        if sync_head:
            _managed_clone_clear_synced_source_ref(repo, branch)
        return

    rc, _, _ = _git_output(
        ["merge-base", "--is-ancestor", local_ref, remote_ref],
        repo,
        timeout=timeout,
    )
    if rc == 0:
        _git_output(["update-ref", local_ref, remote_ref], repo, timeout=timeout)
        _managed_clone_clear_synced_source_ref(repo, branch)
        return

    if sync_head and sync_head == local_head:
        logger.info(
            "workspace: preserving explicitly synced local %s in %s (local=%s remote=%s)",
            branch,
            repo,
            local_head,
            remote_head,
        )
        return

    _git_output(["update-ref", local_ref, remote_ref], repo, timeout=timeout)
    _managed_clone_clear_synced_source_ref(repo, branch)
    logger.info(
        "workspace: reset divergent local %s in %s back to origin (local=%s remote=%s)",
        branch,
        repo,
        local_head,
        remote_head,
    )


def _repo_integration_base_ref(repo: Path) -> str:
    """Return the best base ref for fresh worktrees and cleanup checks.

    Preference order:
    - local default branch when it was explicitly synced from another checkout
    - remote-tracking default branch when local is simply behind upstream
    - ``origin/HEAD`` as a final fallback
    """
    branch = _repo_default_branch(repo)
    if branch:
        local_ref = f"refs/heads/{branch}"
        remote_ref = f"refs/remotes/origin/{branch}"
        local_ok = _git_output(["rev-parse", "--verify", local_ref], repo, timeout=15)[0] == 0
        remote_ok = _git_output(["rev-parse", "--verify", remote_ref], repo, timeout=15)[0] == 0
        if local_ok and remote_ok:
            local_head = _git_output(["rev-parse", "--verify", local_ref], repo, timeout=15)[1].strip()
            remote_head = _git_output(["rev-parse", "--verify", remote_ref], repo, timeout=15)[1].strip()
            if local_head and remote_head and local_head != remote_head:
                sync_head = _managed_clone_synced_source_head(repo, branch)
                if sync_head and sync_head == local_head:
                    return branch
                rc, _, _ = _git_output(["merge-base", "--is-ancestor", local_ref, remote_ref], repo, timeout=15)
                if rc == 0:
                    return f"origin/{branch}"
                return f"origin/{branch}"
            return branch
        if local_ok:
            return branch
        if remote_ok:
            return f"origin/{branch}"
    return "origin/HEAD"


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


def _managed_clone_synced_source_ref(branch: str) -> str:
    """Ref that records an explicit non-origin source for ``branch``."""
    return f"refs/dashboard/synced-source/{branch}"


def _managed_clone_synced_source_head(repo: Path, branch: str) -> str | None:
    """Return the head SHA for the explicit synced-source marker, if any."""
    rc, out, _ = _git_output(
        ["rev-parse", "--verify", _managed_clone_synced_source_ref(branch)],
        repo,
        timeout=15,
    )
    if rc != 0:
        return None
    head = out.strip()
    return head or None


def _managed_clone_clear_synced_source_ref(repo: Path, branch: str) -> None:
    """Drop the explicit synced-source marker when origin resumes ownership."""
    _git_output(
        ["update-ref", "-d", _managed_clone_synced_source_ref(branch)],
        repo,
        timeout=15,
    )


def _sync_managed_clone_branch_ref(
    clone: Path,
    source_repo: Path,
    branch: str,
    *,
    timeout: int = 600,
) -> None:
    """Sync ``clone``'s local branch ref from ``source_repo`` using a temp ref.

    The managed clone can have the destination branch checked out, so fetch into
    a temporary ref first and then advance the local branch with ``update-ref``.
    """
    temp_ref = "refs/tmp_sync"
    try:
        _run_git(
            ["fetch", str(source_repo), f"refs/heads/{branch}:{temp_ref}"],
            cwd=clone,
            timeout=timeout,
        )
        _run_git(
            ["update-ref", f"refs/heads/{branch}", temp_ref],
            cwd=clone,
            timeout=timeout,
        )
        _run_git(
            ["update-ref", _managed_clone_synced_source_ref(branch), temp_ref],
            cwd=clone,
            timeout=timeout,
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


def _worktree_dashboard_base_ref(
    worktree: Path,
    repo_name: str,
    *,
    target_branch_and_head: tuple[str | None, str | None] | None = None,
) -> str | None:
    """Return the review base ref used by the dashboard for a worktree.

    ``target_branch_and_head`` lets a caller that already resolved
    :func:`_autonomy_target_branch_and_head` once (e.g. ``scan_all_worktrees``
    doing it once per sweep instead of once per row) pass it in; standalone
    callers omit it and it's resolved internally, unchanged.
    """
    fallback = _worktree_merge_base_ref(worktree)
    if repo_name != "autonomy":
        return fallback

    _target_branch, target_head = (
        target_branch_and_head if target_branch_and_head is not None
        else _autonomy_target_branch_and_head()
    )
    if not target_head:
        return fallback

    rc, _, _ = _git_output(["merge-base", "--is-ancestor", target_head, "HEAD"], worktree, timeout=15)
    if rc == 0:
        return target_head
    return fallback


def _worktree_commits_ahead(worktree: Path, *, base_ref: str | None = None) -> int:
    """Count commits whose patches are NOT yet on the merge base ref.

    Previously did a plain ``git rev-list --count base..HEAD``, which
    counts SHA-distinct commits. That overstated "ahead" any time a
    commit had already been cherry-picked onto master — the cherry-pick
    has a different SHA but the same patch-id, so the original commit
    on the session branch kept counting forward forever (until the
    branch was rebased).

    ``git cherry <base>`` does patch-id matching: each commit on HEAD is
    annotated with ``+`` (patch-id not in base) or ``-`` (patch-id
    already in base, e.g. a cherry-pick of this commit). Counting only
    the ``+`` lines gives "commits whose work the operator still needs
    to land", which is the intent every caller actually wants:

    - dual-state lit indicator on the session viewer's worktree button
    - ff_eligible (no point fast-forwarding zero meaningful commits)
    - the commit count on the worktree review card

    Merge commits are skipped by ``git cherry`` by design, mirroring the
    old behaviour.
    """
    base_ref = base_ref or _worktree_merge_base_ref(worktree)
    if base_ref is None:
        return 0
    rc, out, _ = _git_output(["cherry", base_ref, "HEAD"], worktree, timeout=15)
    if rc != 0:
        return 0
    return sum(1 for line in out.splitlines() if line.startswith("+ "))


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
    rc, out, _ = _git_output(
        ["status", "--porcelain", "--untracked-files=all"],
        worktree,
        timeout=15,
    )
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


def _worktree_integrated_patch(worktree: Path, base_ref: str | None) -> str | None:
    """Return the unified diff for ``base_ref..HEAD`` (the integrated PR diff).

    This is the same diff a reviewer sees on the PR page: every commit on
    the branch combined into one patch. ``base_ref`` is the worktree's
    merge-base ref (typically ``main``/``master``); when missing we
    can't compute the integrated diff.
    """
    if base_ref is None:
        return None
    rc, out, _ = _git_output(
        ["diff", "--patch", "--find-renames", f"{base_ref}...HEAD"],
        worktree,
        timeout=60,
    )
    if rc != 0:
        return None
    return out.strip()


def _worktree_integrated_numstats(
    worktree: Path, base_ref: str | None,
) -> dict[str, tuple[int, int]]:
    """Numstat per file for the integrated ``base_ref..HEAD`` diff."""
    if base_ref is None:
        return {}
    rc, out, _ = _git_output(
        ["diff", "--numstat", "--find-renames", f"{base_ref}...HEAD"],
        worktree,
        timeout=30,
    )
    if rc != 0:
        return {}
    out_map: dict[str, tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path = parts[-1].strip()
        if not path:
            continue
        out_map[path] = (_parse_numstat(parts[0]), _parse_numstat(parts[1]))
    return out_map


def _worktree_integrated_name_status(
    worktree: Path, base_ref: str | None,
) -> list[GitFileChange]:
    """File-level name/status list for the integrated diff (no additions/deletions)."""
    if base_ref is None:
        return []
    rc, out, _ = _git_output(
        ["diff", "--name-status", "--find-renames", f"{base_ref}...HEAD"],
        worktree,
        timeout=30,
    )
    if rc != 0:
        return []
    files: list[GitFileChange] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        path = parts[-1].strip()
        if not path:
            continue
        files.append(GitFileChange(status=status[:1] or "M", path=path))
    return files


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
    """Return True when ``sha`` (or its patch) is already on ``branch``.

    Checks SHA-reachability first (cheapest) then falls back to patch-id
    equivalence via ``git cherry`` so a commit that was cherry-picked onto
    ``branch`` with a different SHA is still detected as merged. Without
    this, an orphan worktree branch keeps showing its original commit as
    "pending" forever after the cherry-pick lands as a new SHA on master.
    """
    rc, _, _ = _git_output(
        ["merge-base", "--is-ancestor", sha, f"refs/heads/{branch}"],
        repo,
        timeout=15,
    )
    if rc == 0:
        return True
    rc, out, _ = _git_output(
        ["cherry", f"refs/heads/{branch}", sha, f"{sha}^"],
        repo,
        timeout=15,
    )
    if rc != 0:
        return False
    line = (out.splitlines() or [""])[0].strip()
    return line.startswith("-")


def _target_branch_merged_shas(
    repo: Path,
    branch: str,
    head_sha: str,
    candidates: list[str],
) -> set[str]:
    """Classify which of ``candidates`` are merged into ``branch``, in one call.

    Replaces a per-SHA ``_target_branch_contains_commit`` loop (a
    ``merge-base --is-ancestor`` plus a ``git cherry`` patch-id check per
    commit) with a single ``git cherry <branch> <head_sha>``. ``git cherry``
    lists every commit reachable from ``head_sha`` but not ``branch``,
    prefixed ``+`` (patch not yet on branch) or ``-`` (patch-id equivalent
    already on branch, e.g. a cherry-pick with a different SHA). A commit
    that's a direct SHA-identical ancestor of ``branch`` doesn't appear in
    the output at all (nothing unique to report) — also correctly
    classified here as merged, since only ``+`` lines mean "still pending."

    ``candidates`` must all be reachable from ``head_sha`` (e.g. the
    ordered output of :func:`_worktree_commit_shas`, whose tip is
    ``head_sha``). On any git failure, returns an empty set — the same
    conservative "treat as still pending" default the old per-SHA loop
    fell back to when a SHA/branch couldn't be resolved.
    """
    if not candidates:
        return set()
    rc, out, _ = _git_output(
        ["cherry", f"refs/heads/{branch}", head_sha],
        repo,
        timeout=30,
    )
    if rc != 0:
        return set()
    still_pending: set[str] = set()
    for line in out.splitlines():
        marker, _, sha = line.strip().partition(" ")
        if marker == "+":
            still_pending.add(sha.strip())
    candidate_set = set(candidates)
    return candidate_set - still_pending


def _dashboard_pending_commit_shas(
    worktree: Path,
    repo_name: str,
    *,
    base_ref: str | None = None,
    target_branch_and_head: tuple[str | None, str | None] | None = None,
) -> list[str]:
    """Return worktree ahead SHAs that are not already merged into the target repo."""
    pending = _worktree_commit_shas(worktree, base_ref=base_ref)
    if repo_name != "autonomy" or not pending:
        return pending

    target_branch, _target_head = (
        target_branch_and_head if target_branch_and_head is not None
        else _autonomy_target_branch_and_head()
    )
    if target_branch is None:
        return pending

    merged = _target_branch_merged_shas(REPO_ROOT, target_branch, pending[-1], pending)
    return [sha for sha in pending if sha not in merged]


def _worktree_clone_stale(
    repo_name: str,
    clone: Path | None,
    *,
    target_branch_and_head: tuple[str | None, str | None] | None = None,
) -> bool:
    """Return True when the managed clone lags the host integration branch."""
    if repo_name != "autonomy" or clone is None:
        return False

    target_branch, target_head = (
        target_branch_and_head if target_branch_and_head is not None
        else _autonomy_target_branch_and_head()
    )
    if target_branch is None or target_head is None:
        return False

    clone_head = _repo_branch_head(clone, target_branch)
    if clone_head is None:
        return True
    return clone_head != target_head


def _worktree_rebase_required(
    worktree: Path,
    repo_name: str,
    *,
    has_pending_commits: bool,
    clone_stale: bool,
    target_branch_and_head: tuple[str | None, str | None] | None = None,
) -> bool:
    """Return True when the target branch has advanced past the worktree fork point."""
    if repo_name != "autonomy" or not has_pending_commits or clone_stale:
        return False

    _target_branch, target_head = (
        target_branch_and_head if target_branch_and_head is not None
        else _autonomy_target_branch_and_head()
    )
    if not target_head:
        return False

    rc, _, _ = _git_output(["merge-base", "--is-ancestor", target_head, "HEAD"], worktree, timeout=15)
    return rc != 0


def _parse_commit_file_changes(
    numstat_text: str,
    name_status_text: str,
) -> list[GitFileChange]:
    """Parse paired ``--numstat``/``--name-status`` bodies into file changes.

    Shared by the single-commit path (:func:`_commit_file_changes`) and the
    batched per-row path (:func:`_read_worktree_commits_batch`) so both
    produce byte-identical results from the same underlying git data —
    including the pre-existing quirk that a renamed file reports
    additions=0/deletions=0: numstat's compact ``old => new`` rename path
    never matches name-status's bare new-path key, so the lookup misses and
    the trailing "unseen numstat entries" pass appends a second, spurious
    ``status="?"`` row keyed on the arrow-joined path. That's existing
    dashboard behavior; this function preserves it rather than fixing it.

    An empty string for either argument is treated the same whether it
    came from "the git call failed" or "the git call succeeded with no
    output" — both cases produce the same result in the original
    per-commit code (verified by inspection), so no separate rc signal is
    needed here.
    """
    numstats: dict[str, tuple[int, int]] = {}
    for line in numstat_text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path = parts[-1].strip()
        if not path:
            continue
        numstats[path] = (_parse_numstat(parts[0]), _parse_numstat(parts[1]))

    if not name_status_text:
        return [
            GitFileChange(status="?", path=path, additions=adds, deletions=dels)
            for path, (adds, dels) in sorted(numstats.items())
        ]

    files: list[GitFileChange] = []
    seen: set[str] = set()
    for line in name_status_text.splitlines():
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


def _commit_file_changes(worktree: Path, sha: str) -> list[GitFileChange]:
    """Return file-level status and numstat details for one commit."""
    rc, out, _ = _git_output(
        ["show", "--numstat", "--format=", "--find-renames", sha],
        worktree,
        timeout=30,
    )
    numstat_text = out if rc == 0 else ""

    rc, out, _ = _git_output(
        ["show", "--name-status", "--format=", "--find-renames", sha],
        worktree,
        timeout=30,
    )
    name_status_text = out if rc == 0 else ""

    return _parse_commit_file_changes(numstat_text, name_status_text)


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


# Record separator prefixed to each commit's fields in the batched ``git
# log`` calls below, so splitting the whole blob on it yields one clean
# chunk per commit. Field separator matches ``_read_worktree_commit``'s
# single-commit ``--format`` exactly.
_LOG_RECORD_SEP = "\x1e"
_LOG_FIELD_SEP = "\x1f"


def _batch_log_diffstat_chunks(
    worktree: Path,
    diff_flag: str,
    shas: list[str],
) -> dict[str, str]:
    """Run one ``git log <diff_flag> --find-renames`` over all of ``shas``.

    Returns ``{sha: diffstat_text}`` where each value is byte-identical to
    what ``git show <diff_flag> --format= --find-renames <sha>`` would have
    produced for that sha alone — so :func:`_parse_commit_file_changes` can
    consume it unchanged. One call replaces N per-commit ``git show`` calls.
    """
    if not shas:
        return {}
    rc, out, _ = _git_output(
        [
            "log", "--no-walk=unsorted", f"--format={_LOG_RECORD_SEP}%H",
            diff_flag, "--find-renames", *shas,
        ],
        worktree,
        timeout=30,
    )
    if rc != 0:
        return {}
    chunks: dict[str, str] = {}
    for chunk in out.split(_LOG_RECORD_SEP):
        if not chunk:
            continue
        sha, _, rest = chunk.partition("\n")
        chunks[sha.strip()] = rest
    return chunks


def _read_worktree_commits_batch(
    worktree: Path,
    shas: list[str],
) -> dict[str, WorktreeCommit]:
    """Batched equivalent of calling ``_read_worktree_commit`` once per sha.

    Three ``git log`` invocations total for the whole list (header fields,
    numstat, name-status) instead of three ``git show``/``git diff`` style
    invocations PER COMMIT — the dominant cost the sweep redesign targets.
    Patch text is never populated here (``_worktree_commits`` never asked
    ``_read_worktree_commit`` for it either — only the explicit single-commit
    detail readers pass ``include_patch=True``).
    """
    if not shas:
        return {}

    fmt = f"{_LOG_RECORD_SEP}%H{_LOG_FIELD_SEP}%h{_LOG_FIELD_SEP}%an{_LOG_FIELD_SEP}%ad{_LOG_FIELD_SEP}%s{_LOG_FIELD_SEP}%b"
    rc, header_out, _ = _git_output(
        ["log", "--no-walk=unsorted", f"--format={fmt}", "--date=format:%Y-%m-%d %H:%M", *shas],
        worktree,
        timeout=30,
    )
    if rc != 0:
        return {}

    numstat_chunks = _batch_log_diffstat_chunks(worktree, "--numstat", shas)
    name_status_chunks = _batch_log_diffstat_chunks(worktree, "--name-status", shas)

    commits: dict[str, WorktreeCommit] = {}
    for chunk in header_out.split(_LOG_RECORD_SEP):
        if not chunk:
            continue
        parts = chunk.rstrip("\n").split(_LOG_FIELD_SEP, 5)
        if len(parts) != 6:
            continue
        sha = parts[0].strip()
        files = _parse_commit_file_changes(
            numstat_chunks.get(sha, ""),
            name_status_chunks.get(sha, ""),
        )
        commits[sha] = WorktreeCommit(
            sha=sha,
            short_sha=parts[1].strip(),
            author=parts[2].strip(),
            date=parts[3].strip(),
            subject=parts[4].strip(),
            body=parts[5].strip(),
            files=files,
            patch=None,
        )
    return commits


def _worktree_commits(
    worktree: Path,
    repo_name: str,
    *,
    base_ref: str | None = None,
    target_branch_and_head: tuple[str | None, str | None] | None = None,
) -> list[WorktreeCommit]:
    """Return dashboard-pending commit details for one worktree."""
    shas = _dashboard_pending_commit_shas(
        worktree,
        repo_name,
        base_ref=base_ref,
        target_branch_and_head=target_branch_and_head,
    )
    if not shas:
        return []
    by_sha = _read_worktree_commits_batch(worktree, shas)
    # Order follows ``shas`` (oldest ahead-commit first, as before); a sha
    # the batch failed to read (should only happen if the whole git log
    # invocation errored) is dropped, mirroring the old per-commit
    # "skip on None" behavior.
    return [by_sha[sha] for sha in shas if sha in by_sha]


def _workflow_resolved_commits(
    repo_name: str,
    commits: list[WorktreeCommit],
) -> list[WorktreeCommit]:
    """Filter dashboard commits through commit-workflow lifecycle state.

    The workflow store owns the displayed outstanding set. Git topology flags
    are computed separately from the physical branch and must not use this
    filtered subset as their source of truth.

    The input list has already passed through ``_dashboard_pending_commit_shas``.
    For autonomy this means git/patch-id merged commits are removed upstream, so
    the resolver's ``git_merged_shas`` branch is exercised by DAO-level callers
    and future integrations that pass a merged set explicitly.
    """
    if not commits:
        return commits
    try:
        from tools.dashboard.dao.commit_workflow_db import (
            resolve_worktree_outstanding,
        )
    except Exception:
        logger.exception("workspace scan: commit workflow resolver import failed")
        return commits
    try:
        dispositions = resolve_worktree_outstanding(
            repo_slug=repo_name,
            scanned_shas=[commit.sha for commit in commits],
            git_merged_shas=set(),
        )
    except Exception:
        logger.exception(
            "workspace scan: commit workflow resolver failed for repo=%s",
            repo_name,
        )
        return commits
    outstanding = {
        disposition.sha
        for disposition in dispositions
        if disposition.outstanding
    }
    return [commit for commit in commits if commit.sha in outstanding]


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

    # Resolved once per sweep rather than once per row (previously ~2-4
    # identical git spawns per row just to answer "what's the host
    # integration branch/head" — the same answer for every row in this
    # pass since it's a single host-side value, not per-worktree state).
    target_branch_and_head = _autonomy_target_branch_and_head()

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
            # Only scan real git worktrees. A half-provisioned workspace dir
            # (e.g. clones not yet landed) has no `.git`; running git there
            # makes git walk UP to the enclosing autonomy superrepo, and
            # `status --untracked-files=all` then traverses the entire data/
            # tree (every worktree, agent-runs, graph.db, …) — pegging CPU and
            # ballooning RSS until the scan never returns and the dashboard
            # startup hook hangs forever. Skip non-worktree dirs.
            if not (repo_dir / ".git").exists():
                continue
            clone = _find_managed_clone_for_worktree(repo_dir)
            branch = _worktree_branch_name(repo_dir)
            base_ref = _worktree_dashboard_base_ref(
                repo_dir, repo_dir.name,
                target_branch_and_head=target_branch_and_head,
            )
            clone_stale = _worktree_clone_stale(
                repo_dir.name, clone,
                target_branch_and_head=target_branch_and_head,
            )
            dirty_files_or_none = _worktree_dirty_files(repo_dir)
            dirty_files = dirty_files_or_none or []
            # Tracked-only dirty: untracked '??' entries do not block rebase /
            # merge / cherry-pick and conflating them produces misleading
            # "stash or commit uncommitted changes" tooltips on worktrees that
            # only have leftover runtime artifacts.
            tracked_dirty = [f for f in dirty_files if f.status != "??"]
            # Preserve the previous safety behavior: if git status fails,
            # treat the worktree as dirty even though paths are unavailable.
            is_dirty = True if dirty_files_or_none is None else bool(tracked_dirty)
            raw_commits = _worktree_commits(
                repo_dir, repo_dir.name, base_ref=base_ref,
                target_branch_and_head=target_branch_and_head,
            )
            commits = _workflow_resolved_commits(repo_dir.name, raw_commits)
            commits_ahead = len(commits)
            raw_commits_ahead = _worktree_commits_ahead(repo_dir, base_ref=base_ref)
            rebase_required = _worktree_rebase_required(
                repo_dir,
                repo_dir.name,
                has_pending_commits=bool(raw_commits),
                clone_stale=clone_stale,
                target_branch_and_head=target_branch_and_head,
            )
            git_ff_eligible = (
                branch is not None
                and raw_commits_ahead > 0
                and not clone_stale
                and not rebase_required
                and _worktree_ff_only_safe(repo_dir, base_ref=base_ref)
            )
            ff_eligible = git_ff_eligible and commits_ahead > 0
            cherry_pick_eligible, cherry_pick_commit = (
                _compute_cherry_pick_eligibility(
                    repo_name=repo_dir.name,
                    clone=clone,
                    commits=raw_commits,
                    commits_ahead=raw_commits_ahead,
                    ff_eligible=git_ff_eligible,
                    clone_stale=clone_stale,
                    target_branch_and_head=target_branch_and_head,
                )
            )
            if commits_ahead == 0:
                cherry_pick_eligible = False
                cherry_pick_commit = None
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
                rebase_required=rebase_required,
                session_live=session_dir.name in live,
                cherry_pick_eligible=cherry_pick_eligible,
                cherry_pick_commit=cherry_pick_commit,
                commits=commits,
                dirty_files=dirty_files,
            ))

    return out


def _worktree_has_uncommitted_changes(worktree: Path) -> bool:
    """Return True if the worktree has TRACKED-file modifications.

    Untracked files (``git status`` ``??`` lines) do NOT count — they don't
    block rebase, merge, or cherry-pick, and conflating them with real
    uncommitted changes produces misleading "stash or commit" tooltips on
    worktrees that just have leftover runtime artifacts (data/agent-runs/,
    data/experiments.db, etc.) which neither side gitignores.
    """
    return has_working_tree_changes(
        worktree,
        untracked="no",
        timeout=15,
        error_is_dirty=True,
    )


def _compute_cherry_pick_eligibility(
    *,
    repo_name: str,
    clone: Path | None,
    commits: list,
    commits_ahead: int,
    ff_eligible: bool,
    clone_stale: bool,
    target_branch_and_head: tuple[str | None, str | None] | None = None,
) -> tuple[bool, str | None]:
    """Decide whether to surface a "Cherry Pick to <branch>" button.

    Eligibility (initial scope — single ahead commit):
      - autonomy repo (matches existing merge restriction)
      - clone is in sync (clone_stale==False) so the dry-run target is real
      - exactly one commit ahead, and FF is not already possible
      - ``git merge-tree`` returns a clean tree (no ``<<<<<<<`` markers)

    The dry-run is purely tree-level (no working tree / index / HEAD writes).
    Multi-commit cherry-pick is intentionally out of scope for now — those
    cases remain on the rebase path.
    """
    if repo_name != "autonomy" or clone is None:
        return False, None
    if ff_eligible or clone_stale:
        return False, None
    if commits_ahead != 1 or not commits:
        return False, None
    target_branch, _ = (
        target_branch_and_head if target_branch_and_head is not None
        else _autonomy_target_branch_and_head()
    )
    if target_branch is None:
        return False, None
    sha = commits[0].sha
    clean, _ = _cherry_pick_dry_run(clone, sha, target_branch)
    return (clean, sha if clean else None)


def _cherry_pick_dry_run(
    clone: Path, commit_sha: str, target_branch: str,
) -> tuple[bool, str]:
    """Return ``(clean, summary)`` — True if cherry-picking ``commit_sha`` onto
    ``target_branch`` would auto-merge without conflicts.

    Uses ``git merge-tree BASE OURS THEIRS`` (git 2.34+ legacy syntax) on the
    managed clone. The operation is purely tree-level — no working tree, index,
    or HEAD mutation. Conflicts surface as ``<<<<<<<`` markers in stdout.

    The dry-run does not write any state. Safe to call on every worktree row
    refresh.
    """
    rc, out, _ = _git_output(
        ["merge-tree", f"{commit_sha}^", target_branch, commit_sha],
        clone,
        timeout=20,
    )
    if rc != 0:
        return False, f"merge-tree exited {rc}"
    if "<<<<<<<" in out:
        return False, "would conflict"
    return True, "clean"


def _worktree_has_commits_ahead_of_base(worktree: Path) -> bool:
    """Return True if HEAD has commits whose patches are not on the base ref.

    Uses ``git cherry`` patch-id matching, mirroring the dashboard's
    :func:`_worktree_commits_ahead`. Without this, a commit that has been
    cherry-picked onto the base ref with a different SHA stays counted
    forever — the original commit on the session branch is not reachable
    from the cherry-picked SHA, so a raw ``rev-list --count base..HEAD``
    keeps reporting it as ahead and ``cleanup_session_worktrees``
    preserves the worktree past the point where its work has landed.

    If the comparison can't be made (git error), returns True so we
    preserve by default.
    """
    base_ref = _repo_integration_base_ref(worktree)
    rc, out, _ = _git_output(
        ["cherry", base_ref, "HEAD"],
        worktree,
        timeout=15,
    )
    if rc != 0:
        return True
    return any(line.startswith("+ ") for line in out.splitlines())


def _delete_branch(clone: Path, branch: str) -> None:
    """Delete ``branch`` from ``clone``; best-effort, logged on failure."""
    rc, _, err = _git_output(["branch", "-D", branch], clone, timeout=15)
    if rc != 0 and "not found" not in err.lower():
        logger.warning(
            "workspace cleanup: failed to delete branch %s in %s: %s",
            branch, clone, err.strip(),
        )


def _log_worktree_removed(path: Path | str, *, method: str) -> None:
    """Log a worktree-directory removal at INFO with a caller stack snippet.

    Every code path that deletes a worktree under ``data/worktrees/`` MUST
    log via this helper (or call something that does). The stack snippet
    makes it possible to attribute a missing-worktree incident to a
    specific call site months after the fact — without it, a silent
    deletion path leaves no audit trail and root-causing requires
    re-instrumenting every caller. See the 2026-05-02 auto-0502-123849
    incident for the original motivation.
    """
    # Trim the immediate frame (this helper) and any internal trampoline
    # so the first non-noise frame the operator sees is the deletion call.
    stack = traceback.extract_stack()[:-1]
    # Keep the last 4 frames — caller chain is short for these helpers
    # and 4 is enough to identify both the workspace_manager function
    # and the upstream API/dispatcher entry point.
    chain = " ← ".join(f"{f.name}@{Path(f.filename).name}:{f.lineno}" for f in stack[-4:])
    logger.info(
        "workspace cleanup: REMOVED %s  method=%s  caller=%s",
        path, method, chain,
    )


def _worktree_remove(clone: Path, worktree: Path) -> tuple[bool, str]:
    """``git worktree remove --force`` from the managed clone. Returns (ok, err)."""
    rc, _, err = _git_output(
        ["worktree", "remove", str(worktree), "--force"],
        clone,
        timeout=30,
    )
    if rc == 0:
        _log_worktree_removed(worktree, method="git-worktree-remove")
    return rc == 0, err.strip()


def _worktree_prune(clone: Path) -> None:
    """Prune stale worktree metadata in the managed clone."""
    _git_output(["worktree", "prune"], clone, timeout=15)


# Process-lifetime memo of the last logged preserve verdict per worktree
# path. ``prune_orphan_worktrees`` re-derives the same "preserve" decision
# for the same stale worktree every 600s forever — before this, that was a
# WARNING every time (a 45-line burst each prune tick for a stable set of
# old worktrees). Only a NEW or CHANGED verdict is worth an operator's
# attention; an unchanged repeat is DEBUG. Module-level dict is acceptable
# for Phase 0 (no persistence across process restarts needed — a fresh
# process logging the first WARNING again is fine).
_preserve_verdict_seen: dict[str, str] = {}


def _log_preserve_verdict(entry: Path, reason: str) -> None:
    """Log a worktree-preserve verdict, demoted to DEBUG on an exact repeat."""
    key = str(entry)
    if _preserve_verdict_seen.get(key) == reason:
        logger.debug("workspace cleanup: preserving %s (%s)", entry, reason)
        return
    _preserve_verdict_seen[key] = reason
    logger.warning("workspace cleanup: preserving %s (%s)", entry, reason)


def cleanup_session_worktrees(
    session_name: str,
    *,
    force: bool = False,
    worktrees_dir: Path,
) -> CleanupResult:
    """Remove worktrees and branch for ``session_name``.

    Walks ``data/worktrees/{session_name}/`` and, for each repo subdirectory:

    - If the worktree has uncommitted changes or local commits on its
      ``session/{session_name}`` branch, preserve it (unless ``force=True``)
      and record the reason.
    - Otherwise run ``git worktree remove --force`` against the managed
      clone, delete the session branch, and prune the clone's worktree
      metadata.

    The containing ``{session_name}`` directory is removed once empty.
    Returns a :class:`CleanupResult` summarizing what was done.
    """
    _refuse_pytest_against_real_worktrees(worktrees_dir, "cleanup_session_worktrees")
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
            elif _worktree_has_commits_ahead_of_base(entry):
                # Only check commits when the tree is clean — avoids
                # treating a mid-edit worktree as "local commits".
                reasons.append("local commits")
            if reasons:
                reason = ", ".join(reasons)
                result.preserved.append((str(entry), reason))
                _log_preserve_verdict(entry, reason)
                continue

        if clone is None:
            # Stale worktree with no resolvable clone — remove the directory.
            try:
                _force_rmtree(entry)
                _log_worktree_removed(entry, method="rmtree-no-clone")
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
                    _force_rmtree(entry)
                    _log_worktree_removed(entry, method="rmtree-fallback")
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
                _log_worktree_removed(session_dir, method="rmdir-empty-parent")
            except OSError as e:
                result.errors.append((str(session_dir), f"rmdir: {e}"))

    # Delete the session branch and prune stale worktree metadata in each
    # managed clone we touched. Both are best-effort — a failure here does
    # not roll back the removed worktrees.
    for clone in clones_touched:
        _delete_branch(clone, branch)
        _worktree_prune(clone)

    # Wipe operator-declared review bindings for this session — they're
    # scoped to the worktree and would dangle otherwise. The
    # ``review_state`` cache is per-org and survives this on purpose so
    # the next worktree against the same review re-uses it.
    _delete_review_bindings_for_session(session_name)

    return result


def cleanup_session_worktree(
    session_name: str,
    repo_name: str,
    *,
    force: bool = False,
    worktrees_dir: Path,
) -> CleanupResult:
    """Remove one repo worktree for ``session_name`` while preserving others."""
    _refuse_pytest_against_real_worktrees(worktrees_dir, "cleanup_session_worktree")
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
        elif _worktree_has_commits_ahead_of_base(entry):
            reasons.append("local commits")
        if reasons:
            result.preserved.append((str(entry), ", ".join(reasons)))
            logger.warning(
                "workspace cleanup: preserving %s (%s)",
                entry, ", ".join(reasons),
            )
            return result

    if clone is None:
        try:
            _force_rmtree(entry)
            _log_worktree_removed(entry, method="rmtree-no-clone")
            result.removed.append(str(entry))
        except OSError as e:
            result.errors.append((str(entry), f"rmtree: {e}"))
            return result
    else:
        ok, err = _worktree_remove(clone, entry)
        if not ok:
            if entry.exists():
                try:
                    _force_rmtree(entry)
                    _log_worktree_removed(entry, method="rmtree-fallback")
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
                _log_worktree_removed(session_dir, method="rmdir-empty-parent")
            except OSError as e:
                result.errors.append((str(session_dir), f"rmdir: {e}"))

    # Wipe operator-declared review bindings scoped to this exact
    # (session, repo) pair. The cache stays put.
    _delete_review_bindings_for_session_repo(session_name, repo_name)

    return result


def _delete_review_bindings_for_session(session_name: str) -> int:
    """Hard-delete every review-binding row scoped to ``session_name``."""
    try:
        from tools.graph import settings_ops
        from tools.graph.schemas.worktree_review_binding import (
            SET_ID as REVIEW_BINDING_SET_ID,
        )
    except Exception:
        logger.warning(
            "workspace cleanup: settings_ops unavailable; skipping "
            "review binding cleanup for %s",
            session_name,
        )
        return 0
    try:
        return settings_ops.remove_settings_by_key_prefix(
            REVIEW_BINDING_SET_ID,
            prefix=session_name,
            org="autonomy",
        )
    except Exception:
        logger.warning(
            "workspace cleanup: review binding cleanup failed for %s",
            session_name,
            exc_info=True,
        )
        return 0


def _delete_review_bindings_for_session_repo(
    session_name: str,
    repo_name: str,
) -> int:
    """Hard-delete every review-binding row scoped to ``(session, repo)``."""
    try:
        from tools.graph import settings_ops
        from tools.graph.schemas.worktree_review_binding import (
            SET_ID as REVIEW_BINDING_SET_ID,
        )
    except Exception:
        return 0
    try:
        return settings_ops.remove_settings_by_key_prefix(
            REVIEW_BINDING_SET_ID,
            prefix=f"{session_name}:{repo_name}",
            org="autonomy",
        )
    except Exception:
        logger.warning(
            "workspace cleanup: review binding cleanup failed for %s/%s",
            session_name,
            repo_name,
            exc_info=True,
        )
        return 0


def prune_orphan_worktrees(
    live_session_names: Iterable[str],
    *,
    force: bool = False,
    worktrees_dir: Path,
) -> dict[str, CleanupResult]:
    """Clean worktrees for sessions no longer in ``live_session_names``.

    Scans ``worktrees_dir`` and calls :func:`cleanup_session_worktrees` for
    each subdirectory whose name is not in ``live_session_names``.
    Returns a mapping of ``session_name → CleanupResult``.
    """
    _refuse_pytest_against_real_worktrees(worktrees_dir, "prune_orphan_worktrees")
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
    ff_eligible = commits_ahead > 0 and _worktree_ff_only_safe(worktree, base_ref=base_ref)
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


def cherry_pick_session_worktree(
    session_name: str,
    repo_name: str,
    *,
    worktrees_dir: Path = WORKTREES_DIR,
) -> dict[str, str]:
    """Apply a single ahead commit from a session worktree onto host ``target_branch``.

    Pre-checked eligibility (one-ahead + clean merge-tree dry-run + autonomy +
    not stale) is recomputed before commit so a stale row in the UI cannot
    drive a cherry-pick that would conflict.

    On any failure during ``git cherry-pick`` (rare given the pre-check, but
    possible if state changed between dry-run and apply), the in-progress
    cherry-pick is aborted so the host repo is left in its prior state.
    """
    worktree = worktrees_dir / session_name / repo_name
    if not worktree.exists() or not worktree.is_dir():
        raise WorkspaceError(f"worktree not found: {worktree}")

    clone = _find_managed_clone_for_worktree(worktree)
    if clone is None:
        raise WorkspaceError(f"managed clone not found for worktree: {worktree}")

    branch = _worktree_branch_name(worktree)
    if branch is None:
        raise WorkspaceError(f"worktree is detached or unreadable: {worktree}")

    if repo_name != "autonomy":
        raise WorkspaceError(
            f"cherry-pick unsupported for repo {repo_name!r}; "
            "only 'autonomy' can be cherry-picked from the dashboard today"
        )

    base_ref = _worktree_dashboard_base_ref(worktree, repo_name)
    commits = _worktree_commits(worktree, repo_name, base_ref=base_ref)
    commits_ahead = _worktree_commits_ahead(worktree, base_ref=base_ref)
    clone_stale = _worktree_clone_stale(repo_name, clone)
    ff_eligible = (
        commits_ahead > 0
        and not clone_stale
        and _worktree_ff_only_safe(worktree, base_ref=base_ref)
    )
    eligible, sha = _compute_cherry_pick_eligibility(
        repo_name=repo_name,
        clone=clone,
        commits=commits,
        commits_ahead=commits_ahead,
        ff_eligible=ff_eligible,
        clone_stale=clone_stale,
    )
    if not eligible or sha is None:
        raise WorkspaceError(
            f"worktree {session_name}/{repo_name} is not cherry-pick eligible "
            f"(ahead={commits_ahead}, ff_eligible={ff_eligible}, "
            f"clone_stale={clone_stale})"
        )

    target_branch, _ = _autonomy_target_branch_and_head()
    if target_branch is None:
        raise WorkspaceError("could not determine autonomy integration branch")

    target_repo = REPO_ROOT
    rc, head_branch, _ = _git_output(
        ["symbolic-ref", "--quiet", "--short", "HEAD"], target_repo, timeout=15,
    )
    if rc != 0 or head_branch.strip() != target_branch:
        raise WorkspaceError(
            f"host HEAD must be on {target_branch} to cherry-pick "
            f"(currently on {head_branch.strip() or 'detached'!r})"
        )

    # Auto-stash any uncommitted host edits so cherry-pick can apply.
    # Host working trees are *live* (uvicorn --reload watches the same
    # files), so leaving the operator with no remediation but "stash or
    # commit before cherry-picking" silently regressed deployed
    # behaviour every time someone had unsaved tweaks. We stash, run
    # the cherry-pick, then pop — mirroring the recovery logic in
    # ``agents/dispatcher.py:merge_branch`` so both paths handle dirty
    # trees the same way. Untracked files don't count
    # (``_worktree_has_uncommitted_changes`` already filters them).
    stashed = False
    if _worktree_has_uncommitted_changes(target_repo):
        rc, _, stash_err = _git_output(
            ["stash", "push", "-m",
             f"dispatcher-auto-stash-cherry-pick-{session_name}"],
            target_repo, timeout=15,
        )
        if rc != 0:
            raise WorkspaceError(
                f"host working tree has uncommitted tracked changes and "
                f"git stash failed: {stash_err.strip()}"
            )
        stashed = True

    fetched_sha: str | None = None
    cherry_landed = False
    try:
        _run_git(["fetch", str(clone), branch], cwd=target_repo)
        fetched_sha = _run_git(
            ["rev-parse", "FETCH_HEAD"], cwd=target_repo,
        ).strip()

        rc, cherry_out, cherry_err = _git_output(
            ["cherry-pick", fetched_sha], target_repo, timeout=60,
        )
        if rc != 0:
            # Apply failed despite clean dry-run — rare, but possible
            # if the repo state shifted between probe and apply. Abort
            # so the host tree returns to its prior state, then surface
            # the error. The ``finally`` below pops our stash, so the
            # operator's edits are restored before the raise propagates.
            _git_output(["cherry-pick", "--abort"], target_repo, timeout=15)
            raise WorkspaceError(
                f"cherry-pick failed: {cherry_err.strip() or cherry_out.strip()}"
            )
        cherry_landed = True

        new_commit = _run_git(
            ["rev-parse", "--verify", f"refs/heads/{target_branch}"],
            cwd=target_repo,
        ).strip()
        _sync_managed_clone_branch_ref(clone, target_repo, target_branch)
        message = _run_git(
            ["log", "-1", "--pretty=%s", new_commit], cwd=target_repo,
        ).strip()
        return {
            "commit": new_commit,
            "source_commit": sha,
            "message": message,
            "target_repo": str(target_repo),
            "target_branch": target_branch,
        }
    finally:
        # Restore the host's working-tree edits unconditionally so we
        # don't quietly leave the host stripped across an error path.
        if stashed:
            rc, _, pop_err = _git_output(
                ["stash", "pop"], target_repo, timeout=15,
            )
            if rc != 0 and cherry_landed:
                # Pop conflicts with the just-cherry-picked commit. The
                # host's local edits and the picked commit touch the
                # same hunks. Revert the cherry-pick so the host's
                # edits can be popped back; the operator must
                # re-trigger after resolving.
                _git_output(
                    ["reset", "--hard", "HEAD~1"], target_repo, timeout=15,
                )
                _git_output(["stash", "pop"], target_repo, timeout=15)
                raise WorkspaceError(
                    f"STASH_POP_CONFLICT: cherry-pick of "
                    f"{(fetched_sha or '?')[:8]} succeeded but host "
                    f"edits conflict with the picked code. Reverted "
                    f"cherry-pick to restore host state. Resolve the "
                    f"host edits and re-trigger. ({pop_err.strip()})"
                )
            # If the cherry-pick had already raised (cherry_landed=False)
            # and pop ALSO failed, we leave the stash in place. The
            # cherry-pick error is already propagating; the stash sits
            # in ``git stash list`` for the operator to recover
            # manually. Don't shadow the original error from finally.


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
    info["is_dirty"] = _worktree_has_uncommitted_changes(worktree)
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


_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's canonical empty tree


def sign_request_diff(
    session_name: str,
    repo_name: str,
    parent_sha: str | None,
    tree_sha: str,
    *,
    worktrees_dir: Path = WORKTREES_DIR,
) -> dict:
    """Files + unified patch for a commit that is not yet in history.

    A commit blocked awaiting its signature has no SHA, but git has already
    written its tree object, so the change can be diffed live from the payload's
    parent and tree — the same subprocess-git-on-the-worktree pattern the commit
    overlay already uses. Returns ``{"files": [{"status","path"}], "patch": str}``.
    A root commit (no parent) diffs against the empty tree.
    """
    worktree = _session_worktree_path(session_name, repo_name, worktrees_dir=worktrees_dir)
    base = parent_sha or _EMPTY_TREE
    # per-file add/delete counts, so the overlay's "+N -N" summary renders
    counts: dict[str, tuple[int, int]] = {}
    rc_num, numstat, _ = _git_output(
        ["diff-tree", "-r", "--numstat", "--find-renames", base, tree_sha], worktree
    )
    if rc_num == 0:
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                add = 0 if parts[0] == "-" else int(parts[0])
                dele = 0 if parts[1] == "-" else int(parts[1])
                counts[parts[-1]] = (add, dele)
    rc_ns, name_status, _ = _git_output(
        ["diff-tree", "-r", "--name-status", "--find-renames", base, tree_sha], worktree
    )
    files = []
    if rc_ns == 0:
        for line in name_status.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                path = parts[-1]
                add, dele = counts.get(path, (0, 0))
                files.append({"status": parts[0], "path": path,
                              "additions": add, "deletions": dele})
    rc_p, patch, _ = _git_output(
        ["diff-tree", "-p", "--find-renames", base, tree_sha], worktree
    )
    return {"files": files, "patch": patch if rc_p == 0 else ""}


def get_repo_commit_detail(
    repo_path: Path,
    sha: str,
    *,
    include_patch: bool = True,
) -> WorktreeCommit:
    """Read one commit (subject, body, files, optionally patch) from any repo path.

    Used by the activity-feed worktree-merge diff overlay (auto-24a60):
    after a merge to master, the commit is no longer in any session
    worktree's "ahead range" (which is what
    :func:`get_session_worktree_commit_detail` requires), but it is in
    the main repo's history. This wrapper shares the same reader
    plumbing without the ahead-range gate.
    """
    if not repo_path.exists():
        raise WorkspaceError(f"repo path does not exist: {repo_path}")
    resolved = _resolve_worktree_commit(repo_path, sha)
    commit = _read_worktree_commit(repo_path, resolved, include_patch=include_patch)
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


def get_session_worktree_integrated_diff(
    session_name: str,
    repo_name: str,
    *,
    base_sha: str | None = None,
    head_sha: str | None = None,
    worktrees_dir: Path = WORKTREES_DIR,
) -> WorktreeDirtyDetail:
    """Return the integrated PR diff for one worktree.

    Default is ``merge-base..HEAD``: the operator-classic "everything
    on this branch" diff. When ``base_sha`` and ``head_sha`` are both
    supplied, the diff is scoped to ``base_sha..head_sha`` so stacked
    reviews render only their own commits.

    When the explicit base/head SHAs aren't present locally, the
    return value carries ``stale=True`` with a human-readable
    ``reason`` instead of raising — the UI surfaces a refresh-needed
    indicator instead of silently diffing the wrong range.
    """
    worktree = _session_worktree_path(
        session_name,
        repo_name,
        worktrees_dir=worktrees_dir,
    )
    if base_sha and head_sha:
        if not _worktree_has_object(worktree, base_sha) or not _worktree_has_object(
            worktree, head_sha
        ):
            return WorktreeDirtyDetail(
                files=[],
                patch=None,
                stale=True,
                reason="cached SHA not present locally; refresh required",
            )
        return _diff_for_explicit_shas(worktree, base_sha, head_sha)
    base_ref = _worktree_dashboard_base_ref(worktree, repo_name)
    if base_ref is None:
        raise WorkspaceError(
            f"could not resolve base ref for worktree: {worktree}",
        )
    name_status = _worktree_integrated_name_status(worktree, base_ref)
    numstats = _worktree_integrated_numstats(worktree, base_ref)
    files = [
        GitFileChange(
            status=f.status,
            path=f.path,
            additions=numstats.get(f.path, (0, 0))[0],
            deletions=numstats.get(f.path, (0, 0))[1],
        )
        for f in name_status
    ]
    return WorktreeDirtyDetail(
        files=files,
        patch=_worktree_integrated_patch(worktree, base_ref),
    )


def _worktree_has_object(worktree: Path, sha: str) -> bool:
    """Return True iff ``sha`` resolves to a commit object in the repo."""
    if not sha:
        return False
    rc, _, _ = _git_output(
        ["cat-file", "-e", f"{sha}^{{commit}}"],
        worktree,
        timeout=10,
    )
    return rc == 0


def _diff_for_explicit_shas(
    worktree: Path,
    base_sha: str,
    head_sha: str,
) -> WorktreeDirtyDetail:
    """Compute unified diff + file metadata for ``base_sha..head_sha``."""
    rc, patch, _ = _git_output(
        ["diff", "--patch", "--find-renames", f"{base_sha}..{head_sha}"],
        worktree,
        timeout=60,
    )
    if rc != 0:
        return WorktreeDirtyDetail(files=[], patch=None, stale=True, reason="git diff failed")
    rc, name_status_out, _ = _git_output(
        ["diff", "--name-status", "--find-renames", f"{base_sha}..{head_sha}"],
        worktree,
        timeout=30,
    )
    rc2, numstat_out, _ = _git_output(
        ["diff", "--numstat", "--find-renames", f"{base_sha}..{head_sha}"],
        worktree,
        timeout=30,
    )
    if rc != 0 or rc2 != 0:
        return WorktreeDirtyDetail(
            files=[],
            patch=patch.strip() or None,
            stale=True,
            reason="git metadata read failed",
        )
    numstats: dict[str, tuple[int, int]] = {}
    for line in numstat_out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path = parts[-1].strip()
        if not path:
            continue
        numstats[path] = (_parse_numstat(parts[0]), _parse_numstat(parts[1]))
    files: list[GitFileChange] = []
    for line in name_status_out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        path = parts[-1].strip()
        if not path:
            continue
        adds, dels = numstats.get(path, (0, 0))
        files.append(
            GitFileChange(
                status=status[:1] or "M",
                path=path,
                additions=adds,
                deletions=dels,
            )
        )
    return WorktreeDirtyDetail(files=files, patch=patch.strip() or None)

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
    # Same rationale as cherry_pick_session_worktree: auto-stash host
    # edits so ff-merge doesn't fail on a dirty working tree, then pop
    # back. ``update-ref`` is dirty-tree-safe so we only stash when
    # we're actually going to call ``git merge``.
    on_target_branch = rc == 0 and head_branch.strip() == target_branch
    stashed = False
    if on_target_branch and _worktree_has_uncommitted_changes(target_repo):
        rc_stash, _, stash_err = _git_output(
            ["stash", "push", "-m",
             f"dispatcher-auto-stash-merge-{session_name}"],
            target_repo, timeout=15,
        )
        if rc_stash != 0:
            raise WorkspaceError(
                f"host working tree has uncommitted tracked changes and "
                f"git stash failed: {stash_err.strip()}"
            )
        stashed = True

    merge_landed = False
    try:
        if on_target_branch:
            _run_git(["merge", "--ff-only", resolved], cwd=target_repo)
        else:
            _run_git(
                ["update-ref", f"refs/heads/{target_branch}", resolved, current_head],
                cwd=target_repo,
            )
        merge_landed = True

        commit = _run_git(
            ["rev-parse", "--verify", f"refs/heads/{target_branch}"],
            cwd=target_repo,
        ).strip()
        _sync_managed_clone_branch_ref(clone, target_repo, target_branch)
        message = _run_git(
            ["log", "-1", "--pretty=%s", commit], cwd=target_repo,
        ).strip()
        return {
            "commit": commit,
            "message": message,
            "target_repo": str(target_repo),
            "target_branch": target_branch,
        }
    finally:
        if stashed:
            rc_pop, _, pop_err = _git_output(
                ["stash", "pop"], target_repo, timeout=15,
            )
            if rc_pop != 0 and merge_landed:
                _git_output(
                    ["reset", "--hard", "HEAD~1"], target_repo, timeout=15,
                )
                _git_output(["stash", "pop"], target_repo, timeout=15)
                raise WorkspaceError(
                    f"STASH_POP_CONFLICT: ff-merge to {resolved[:8]} "
                    f"succeeded but host edits conflict with the merged "
                    f"code. Reverted merge to restore host state. "
                    f"({pop_err.strip()})"
                )
