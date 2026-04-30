"""``autonomy/github`` capability service — typed deterministic surface.

This module is the canonical implementation of the ``autonomy/github``
capability (see ``agents/capabilities/github/manifest.json``). It exposes
the deterministic operations under the ``source_control@1`` contract that
Dashboard, Worktrees, and other consumers depend on, scoped to a single
live ``(session_name, repo_name)`` worktree row.

Operations are async and run as ``docker exec <live-container> gh ...``
against the workspace container that already backs the row — no new
containers, no tmux/agent mediation, no arbitrary shell. Privileged
GitHub access stays inside the container; the dashboard never sees raw
credentials.

Each operation returns a :class:`WorktreeGithubExecResult` with
``ok / stdout / stderr / exit_code / timed_out`` plus execution-context
metadata (operation name, container name, branch, repo slug, command
argv, canonical failure code, error message). Failure codes are a closed
set (see ``FAILURE_*`` constants) so callers can branch on missing-row,
missing-container, missing-``gh``, and missing-auth states without
parsing free-form stderr.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agents.workspace_manager import WorktreeState, parse_repo_url

logger = logging.getLogger(__name__)


# Operation names for the ``source_control@1`` contract surface this
# service implements. Underscore-flat naming matches the contract schema's
# snake_case rule; the displayed surface is ``source_control.review.read``,
# ``source_control.review.refresh``, and ``source_control.gates.watch_set``.
OP_REVIEW_READ = "source_control_review_read_v1"
OP_REVIEW_REFRESH = "source_control_review_refresh_v1"
OP_GATES_WATCH_SET = "source_control_gates_watch_set_v1"

# PR watch modes accepted by ``source_control_gates_watch_set_v1``. They mirror the
# GitHub subscription tri-state — subscribed / ignored / default (cleared).
PR_WATCH_SUBSCRIBED = "subscribed"
PR_WATCH_IGNORED = "ignored"
PR_WATCH_DEFAULT = "default"
PR_WATCH_MODES = frozenset({PR_WATCH_SUBSCRIBED, PR_WATCH_IGNORED, PR_WATCH_DEFAULT})

# Canonical failure codes. ``ok`` is True iff ``failure is None``.
FAILURE_NO_LIVE_ROW = "no_live_row"
FAILURE_NO_LIVE_CONTAINER = "no_live_container"
FAILURE_NO_BRANCH = "no_branch"
FAILURE_NO_REPO_SLUG = "no_repo_slug"
FAILURE_INVALID_MODE = "invalid_mode"
FAILURE_GH_MISSING = "gh_missing"
FAILURE_AUTH_MISSING = "auth_missing"
FAILURE_TIMED_OUT = "timed_out"
FAILURE_EXEC_FAILED = "exec_failed"

# Fields requested from ``gh pr list --head <branch>`` for review
# read/refresh. ``commits`` is the per-PR commit list; ``baseRefOid`` is
# the base commit SHA (so per-PR diffs can be scoped to that PR's range
# rather than the whole branch). ``baseRefName`` lets the UI render the
# integration target. ``statusCheckRollup`` carries gates; ``isDraft``
# / ``mergeable`` / ``reviewDecision`` feed the aggregate-state rule.
PR_LIST_FIELDS = (
    "number,state,title,body,url,"
    "headRefName,headRefOid,baseRefName,baseRefOid,"
    "isDraft,mergeable,mergeStateStatus,reviewDecision,"
    "statusCheckRollup,commits,updatedAt"
)
# Backward-compat alias — kept so any out-of-tree caller importing
# ``PR_VIEW_FIELDS`` (none in tree) still resolves to the new fields.
PR_VIEW_FIELDS = PR_LIST_FIELDS

DEFAULT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class WorktreeGithubExecResult:
    """Structured outcome of one row-scoped GitHub operation.

    ``ok`` is the single source of truth for "did this operation succeed";
    ``failure`` (when non-None) carries the canonical reason. The remaining
    fields capture the docker-exec context so callers can render diagnostics
    or audit the command without re-running it.
    """

    operation: str
    session_name: str
    repo_name: str
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    container_name: str | None = None
    branch: str | None = None
    repo_slug: str | None = None
    command: list[str] = field(default_factory=list)
    failure: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict:
        """JSON-friendly dict for HTTP/SSE surfaces."""
        return {
            "operation": self.operation,
            "session_name": self.session_name,
            "repo_name": self.repo_name,
            "ok": self.ok,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "container_name": self.container_name,
            "branch": self.branch,
            "repo_slug": self.repo_slug,
            "command": list(self.command),
            "failure": self.failure,
            "error_message": self.error_message,
        }


# ── Review payload normalization ──────────────────────────────────────
#
# The Dashboard does not consume raw ``gh pr view --json`` output. It
# composes a typed ``review`` block per the source_control snapshot
# shape (auto-4ze9o), with a ``green``/``yellow`` aggregate and a
# separate ``running`` overlay. ``normalize_review_payload`` maps gh's
# rollup into that shape so callers don't have to know which rollup
# entries carry ``state`` vs ``status``+``conclusion``.
#
# Return shapes are typed dataclasses with ``.to_dict()`` at the JSON
# boundary. This is the transitional shape ahead of auto-0425-010430's
# typed-fields-on-SettingSchema substrate (Bead 1A) — when that lands,
# these dataclasses become the canonical declaration and ``.to_dict()``
# is generated from the same metadata. Zero-rework migration.


_CHECK_PASS = "pass"
_CHECK_FAIL = "fail"
_CHECK_RUNNING = "running"
_CHECK_PENDING = "pending"

_AGGREGATE_GREEN = "green"
_AGGREGATE_YELLOW = "yellow"


@dataclass(frozen=True)
class CheckEntry:
    """One normalized merge-gate check (CheckRun or StatusContext)."""

    id: str
    icon: str
    label: str
    status: str  # one of ``pass`` / ``fail`` / ``running`` / ``pending``
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "icon": self.icon,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReviewPayload:
    """Normalized review block for one PR.

    ``base_sha`` and ``commit_shas`` enable stacked-PR support
    (multiple PRs on one branch — Graphite/Sapling style). For a single
    PR ``base_sha`` is the merge-base with ``base_branch``; for a
    stacked PR ``base_sha`` is the previous PR's head_sha. Per-PR
    diffs are scoped via ``git diff base_sha...head_sha``.
    ``commit_shas`` are the local SHAs in ``rev-list base..HEAD`` order
    that this PR claims.
    """

    number: int | None
    url: str
    title: str
    body: str
    head_sha: str
    base_sha: str
    base_branch: str
    state: str  # ``open`` / ``closed`` / ``merged`` / ``unknown``
    is_draft: bool
    aggregate_state: str  # ``green`` / ``yellow``
    running: bool
    checks: tuple[CheckEntry, ...] = ()
    commit_shas: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "url": self.url,
            "title": self.title,
            "body": self.body,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "base_branch": self.base_branch,
            "state": self.state,
            "is_draft": self.is_draft,
            "aggregate_state": self.aggregate_state,
            "running": self.running,
            "checks": [c.to_dict() for c in self.checks],
            "commit_shas": list(self.commit_shas),
        }


def _icon_from_label(label: str) -> str:
    """Derive a short glyph (1–2 chars) from a check label.

    The Worktrees navigator renders one small disc per check with an
    icon glyph inside (see settled design 3435e03f). gh's check names
    are typically single tokens (``build``, ``test``, ``lint``) but
    sometimes carry a path prefix like ``ci/circleci``. Strip the
    prefix, uppercase the first alpha char, and grab a second from the
    next token if there is one — that gives operator-recognizable
    glyphs (``BL`` for build/lint, ``CC`` for ci/circleci) without
    forcing the capability surface to know about every CI provider.
    """
    text = (label or "").strip()
    if not text:
        return "?"
    # Path prefix: keep the segment after the last slash or colon.
    for sep in ("/", ":"):
        if sep in text:
            text = text.rsplit(sep, 1)[-1]
    text = text.strip()
    if not text:
        return "?"
    # First two alpha characters from the leading word(s).
    parts = [p for p in text.replace("_", " ").replace("-", " ").split() if p]
    if not parts:
        return "?"
    first = next((c for c in parts[0] if c.isalpha()), "")
    second = ""
    if len(parts) >= 2:
        second = next((c for c in parts[1] if c.isalpha()), "")
    glyph = (first + second).upper()
    return glyph[:2] or "?"


def _normalize_check(entry: dict) -> CheckEntry | None:
    """Map one ``statusCheckRollup`` entry to a :class:`CheckEntry`.

    ``gh`` returns two flavors of rollup entry:
      - check runs: ``{__typename: 'CheckRun', name, status, conclusion, ...}``
      - status contexts: ``{__typename: 'StatusContext', context, state, ...}``
    Older gh versions omit ``__typename`` and we have to sniff fields.
    Returns ``None`` for entries we can't classify (defensive, never raises).
    """
    if not isinstance(entry, dict):
        return None

    # Pull a stable label and id no matter which flavor.
    label = entry.get("name") or entry.get("context") or entry.get("title") or ""
    label = str(label).strip() or "(unnamed check)"
    entry_id = entry.get("id") or entry.get("name") or entry.get("context") or label
    icon = _icon_from_label(label)

    detail = entry.get("description") or entry.get("title") or None
    if detail is not None:
        detail = str(detail).strip()[:500] or None

    def make(status: str) -> CheckEntry:
        return CheckEntry(id=entry_id, icon=icon, label=label, status=status, detail=detail)

    # Check-run flavor: status + conclusion.
    status = entry.get("status")
    if status is not None:
        status_norm = str(status).upper()
        if status_norm == "COMPLETED":
            conclusion = str(entry.get("conclusion") or "").upper()
            if conclusion == "SKIPPED":
                return None
            if conclusion == "SUCCESS":
                return make(_CHECK_PASS)
            return make(_CHECK_FAIL)
        if status_norm in {"IN_PROGRESS", "PENDING"}:
            return make(_CHECK_RUNNING)
        if status_norm == "QUEUED":
            return make(_CHECK_PENDING)

    # Status-context flavor: state.
    state = entry.get("state")
    if state is not None:
        state_norm = str(state).upper()
        if state_norm == "SUCCESS":
            return make(_CHECK_PASS)
        if state_norm in {"FAILURE", "ERROR"}:
            return make(_CHECK_FAIL)
        if state_norm == "PENDING":
            return make(_CHECK_PENDING)

    return None


def _normalize_one_pr(data: dict) -> ReviewPayload | None:
    """Shape one PR object from ``gh pr list --json`` into a ReviewPayload.

    Returns ``None`` for non-dict / empty entries. ``commit_shas`` is
    populated from gh's ``commits`` field but not yet ordered against
    local history — :func:`_assign_commits_to_reviews` does that.
    ``base_sha`` is filled with ``baseRefOid`` (the PR's base commit
    SHA per gh) and is later overwritten with the previous PR's head
    SHA when a stack is detected.
    """
    if not isinstance(data, dict):
        return None

    rollup = data.get("statusCheckRollup") or []
    checks: list[CheckEntry] = []
    for entry in rollup:
        normalized = _normalize_check(entry)
        if normalized is not None:
            checks.append(normalized)

    aggregate_yellow = (
        any(c.status == _CHECK_FAIL for c in checks)
        or str(data.get("reviewDecision") or "").upper() == "CHANGES_REQUESTED"
        or str(data.get("mergeable") or "").upper() == "CONFLICTING"
    )
    running = any(c.status in (_CHECK_RUNNING, _CHECK_PENDING) for c in checks)

    # gh's ``commits`` field is a list of {oid, messageHeadline, ...}.
    pr_commits = data.get("commits") or []
    commit_shas: list[str] = []
    for commit in pr_commits:
        if not isinstance(commit, dict):
            continue
        oid = commit.get("oid") or commit.get("sha")
        if oid:
            commit_shas.append(str(oid))

    return ReviewPayload(
        number=data.get("number"),
        url=data.get("url") or "",
        title=(data.get("title") or "").strip(),
        body=data.get("body") or "",
        head_sha=data.get("headRefOid") or "",
        base_sha=data.get("baseRefOid") or "",
        base_branch=data.get("baseRefName") or "",
        state=str(data.get("state") or "").lower() or "unknown",
        is_draft=bool(data.get("isDraft")),
        aggregate_state=_AGGREGATE_YELLOW if aggregate_yellow else _AGGREGATE_GREEN,
        running=running,
        checks=tuple(checks),
        commit_shas=tuple(commit_shas),
    )


def _assign_commits_to_reviews(
    reviews: list[ReviewPayload],
    local_shas: list[str],
) -> tuple[ReviewPayload, ...]:
    """Order ``reviews`` by stack position and stamp local commit_shas.

    ``local_shas`` is ``git rev-list --reverse <merge-base>..HEAD`` —
    chronological order on the worktree branch. For each local SHA we
    pick the PR whose ``commit_shas`` claim it; PRs are then ordered
    by the index of their **last** claimed local SHA. That yields the
    operator-intuitive order: PR #N appears before PR #N+1 when N's
    commits land earlier in the branch history.

    Each PR's ``commit_shas`` is rewritten to the local-order subset.
    For stacked PRs we also rewrite ``base_sha`` to the previous PR's
    head SHA (so per-PR diff scoping `git diff base...head` covers
    only that PR's commits, not the whole stack).
    """
    if not reviews:
        return ()

    # Map every local SHA to the PR that claims it. First-claim wins
    # in the rare double-claim case.
    claims: dict[str, ReviewPayload] = {}
    for review in reviews:
        for sha in review.commit_shas:
            if sha in claims:
                continue
            if sha in local_shas:
                claims[sha] = review

    # Group local SHAs by their owning PR, preserving local order.
    grouped: dict[int, list[str]] = {}
    for sha in local_shas:
        owner = claims.get(sha)
        if owner is None:
            continue
        key = id(owner)
        grouped.setdefault(key, []).append(sha)

    # Score each PR by the local index of its last claimed commit.
    # PRs whose commits aren't on the local stack at all (rare —
    # already-merged PRs, etc.) drop to position 0 and sort by gh's
    # original order.
    def score(review: ReviewPayload) -> tuple[int, int]:
        shas = grouped.get(id(review))
        if not shas:
            return (-1, reviews.index(review))
        return (local_shas.index(shas[-1]), 0)

    ordered = sorted(reviews, key=score)

    # Rewrite commit_shas to the local-order subset, then chain
    # base_sha so each PR's diff is scoped to its own commits.
    out: list[ReviewPayload] = []
    prev_head: str = ""
    for review in ordered:
        shas = grouped.get(id(review)) or []
        base_sha = prev_head if (prev_head and shas) else review.base_sha
        out.append(
            ReviewPayload(
                number=review.number,
                url=review.url,
                title=review.title,
                body=review.body,
                head_sha=review.head_sha,
                base_sha=base_sha,
                base_branch=review.base_branch,
                state=review.state,
                is_draft=review.is_draft,
                aggregate_state=review.aggregate_state,
                running=review.running,
                checks=review.checks,
                commit_shas=tuple(shas),
            )
        )
        if shas:
            prev_head = review.head_sha
    return tuple(out)


def normalize_review_stack(
    raw_stdout: str,
    *,
    local_commit_shas: tuple[str, ...] | list[str] = (),
) -> tuple[ReviewPayload, ...]:
    """Shape ``gh pr list --json`` stdout into an ordered review stack.

    Returns an empty tuple when the branch has no open PRs (gh returns
    ``[]``) or stdout is unparseable. ``local_commit_shas`` is
    ``rev-list --reverse <merge-base>..HEAD`` from the worktree;
    supplying it lets us order stacked PRs by branch position and
    chain their per-PR ``base_sha`` for scoped diffs.
    """
    import json

    if not raw_stdout or not raw_stdout.strip():
        return ()
    try:
        data = json.loads(raw_stdout)
    except (TypeError, ValueError):
        return ()
    if not isinstance(data, list):
        return ()

    reviews: list[ReviewPayload] = []
    for entry in data:
        normalized = _normalize_one_pr(entry)
        if normalized is not None:
            reviews.append(normalized)

    return _assign_commits_to_reviews(reviews, list(local_commit_shas))


# Backward-compat shim — drops to the new normalizer's first review
# (``None`` when the stack is empty). Accepts both the new array-shaped
# stdout (``gh pr list``) and the legacy single-object shape
# (``gh pr view``) so callers that haven't migrated yet keep working.
def normalize_review_payload(
    raw_stdout: str,
    *,
    local_commit_shas: tuple[str, ...] | list[str] = (),
) -> ReviewPayload | None:
    import json

    text = (raw_stdout or "").strip()
    if not text:
        return None
    # If the input is a single object (legacy ``gh pr view`` shape),
    # wrap it in an array so the stack normalizer can consume it.
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        text = json.dumps([data])

    stack = normalize_review_stack(text, local_commit_shas=local_commit_shas)
    return stack[0] if stack else None


# ── Subprocess helpers ────────────────────────────────────────────────
#
# Kept local to this module rather than imported from server.py to avoid
# a circular import (server.py imports this module). Tests monkeypatch
# ``run_cli`` on this module to stub docker/gh invocations.


async def run_cli(
    cmd: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, str, int, bool]:
    """Run ``cmd`` async, returning ``(stdout, stderr, returncode, timed_out)``.

    On timeout, kills the process and returns ``("", "<timeout marker>", -1, True)``.
    Never raises for normal subprocess errors — exit codes are reported as-is.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return "", f"executable not found: {exc}", 127, False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace"), stderr.decode(errors="replace"), proc.returncode, False
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return "", f"timeout after {timeout}s", -1, True


# ── Row + container resolution ────────────────────────────────────────


def find_live_worktree_row(
    session_name: str,
    repo_name: str,
    rows: list[WorktreeState],
) -> WorktreeState | None:
    """Return the cached worktree row for ``(session_name, repo_name)`` if live.

    A row qualifies as "live" when ``session_live`` is True — i.e. the
    worktree's owning session is currently registered as live in
    ``dashboard.db``. A row that exists on disk but whose session is dead
    does NOT qualify; GitHub operations require the backing container.

    ``rows`` must be supplied by the caller. The capability service does
    not reach into Dashboard caches; composition is the orchestrator's
    job (see :mod:`tools.dashboard.worktree_monitor`).
    """
    for row in rows:
        if (
            row.session_name == session_name
            and row.repo_name == repo_name
            and row.session_live
        ):
            return row
    return None


async def resolve_live_container(session_name: str, *, timeout: int = 5) -> str | None:
    """Return the container name backing ``session_name`` if it is running.

    The session launcher names each container after its session
    (``docker run --name=<session_name>`` in ``agents/session_launcher.py``),
    so the row's ``session_name`` IS the container name. We still
    ``docker inspect`` to confirm the container exists and is in the running
    state — a dead container could appear in ``dashboard.db`` between live
    poll cycles, and we must not silently exec into a stopped container.
    """
    stdout, _stderr, rc, _timed_out = await run_cli(
        ["docker", "inspect", "-f", "{{.State.Running}}", session_name],
        timeout=timeout,
    )
    if rc == 0 and stdout.strip() == "true":
        return session_name
    return None


def derive_repo_slug(managed_clone: Path | None) -> str | None:
    """Read ``remote.origin.url`` from the managed clone and return ``owner/repo``.

    Runs synchronously (this is a quick local git-config read, not an
    online operation). Returns None if the managed clone path is missing,
    git-config fails, or the URL doesn't parse to a recognized form.
    """
    if managed_clone is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(managed_clone), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    if not url:
        return None
    try:
        _host, slug = parse_repo_url(url)
    except Exception:
        return None
    return slug


# ── Failure classification ────────────────────────────────────────────


_GH_MISSING_PATTERNS = (
    "executable file not found",
    "command not found",
    "not found in $path",
    'oci runtime exec failed: exec failed: unable to start container process: exec: "gh"',
)

_AUTH_MISSING_PATTERNS = (
    "gh auth login",
    "not logged into",
    "authentication required",
    "no token",
    "bad credentials",
    "401 unauthorized",
    "requires authentication",
)


def classify_failure(stdout: str, stderr: str, exit_code: int, timed_out: bool) -> str | None:
    """Map a docker/gh exit to a canonical failure code, or None on success."""
    if timed_out:
        return FAILURE_TIMED_OUT
    if exit_code == 0:
        return None
    haystack = f"{stdout}\n{stderr}".lower()
    if exit_code == 127 or any(p in haystack for p in _GH_MISSING_PATTERNS):
        return FAILURE_GH_MISSING
    if any(p in haystack for p in _AUTH_MISSING_PATTERNS):
        return FAILURE_AUTH_MISSING
    return FAILURE_EXEC_FAILED


def _short_error(stdout: str, stderr: str, fallback: str) -> str:
    """Pick the most informative one-line error message we can show."""
    for stream in (stderr, stdout):
        if stream and stream.strip():
            return stream.strip().splitlines()[0][:500]
    return fallback


# ── Operation dispatch ────────────────────────────────────────────────


def _failure_result(
    operation: str,
    session_name: str,
    repo_name: str,
    *,
    failure: str,
    error_message: str,
    branch: str | None = None,
    repo_slug: str | None = None,
    container_name: str | None = None,
) -> WorktreeGithubExecResult:
    return WorktreeGithubExecResult(
        operation=operation,
        session_name=session_name,
        repo_name=repo_name,
        ok=False,
        stdout="",
        stderr="",
        exit_code=0,
        timed_out=False,
        container_name=container_name,
        branch=branch,
        repo_slug=repo_slug,
        command=[],
        failure=failure,
        error_message=error_message,
    )


def _pr_view_args(branch: str, repo_slug: str) -> list[str]:
    """Fixed gh argv template for review read/refresh.

    Uses ``gh pr list --head <branch>`` rather than ``gh pr view`` so
    branches carrying multiple stacked PRs (Graphite/Sapling) surface
    every PR, not just the most recent one ``gh pr view`` would
    resolve to. Output is a JSON array — the singular case is just a
    one-element list.
    """
    return [
        "pr", "list",
        "--repo", repo_slug,
        "--head", branch,
        "--state", "open",
        "--json", PR_LIST_FIELDS,
    ]


def _pr_watch_set_args(repo_slug: str, mode: str) -> list[str]:
    """Fixed gh argv template for watch_set.

    Substrate-level note: ``gh`` has no first-class "watch a single PR"
    primitive. The closest fixed-template invocation is the GitHub
    notifications subscription endpoint scoped to the repo, which is what
    GitHub's UI surface ultimately writes when a user toggles watch on a
    repo containing a PR they care about. PR-level (notification-thread)
    subscription requires a thread id that is only minted after activity,
    so it can't be a deterministic single-call template here. A follow-up
    bead can refine this once thread discovery lands.
    """
    if mode == PR_WATCH_SUBSCRIBED:
        return [
            "api", "-X", "PUT", f"/repos/{repo_slug}/subscription",
            "-f", "subscribed=true", "-f", "ignored=false",
        ]
    if mode == PR_WATCH_IGNORED:
        return [
            "api", "-X", "PUT", f"/repos/{repo_slug}/subscription",
            "-f", "subscribed=false", "-f", "ignored=true",
        ]
    # default: clear the subscription
    return ["api", "-X", "DELETE", f"/repos/{repo_slug}/subscription"]


async def _execute_op(
    *,
    operation: str,
    session_name: str,
    repo_name: str,
    rows: list[WorktreeState],
    gh_args_for: callable,
    timeout: int,
    mode: str | None = None,
    require_branch: bool = True,
) -> WorktreeGithubExecResult:
    """Resolve the row + container, build gh argv, exec, classify."""
    row = find_live_worktree_row(session_name, repo_name, rows)
    if row is None:
        return _failure_result(
            operation, session_name, repo_name,
            failure=FAILURE_NO_LIVE_ROW,
            error_message=(
                f"no live worktree row for ({session_name!r}, {repo_name!r}); "
                "the session must be live and the row present in the worktree cache"
            ),
        )

    if require_branch and not row.branch:
        return _failure_result(
            operation, session_name, repo_name,
            failure=FAILURE_NO_BRANCH,
            error_message=f"worktree row for {repo_name!r} has no branch checked out",
        )

    repo_slug = derive_repo_slug(row.managed_clone)
    if repo_slug is None:
        return _failure_result(
            operation, session_name, repo_name,
            failure=FAILURE_NO_REPO_SLUG,
            branch=row.branch,
            error_message=(
                f"could not derive owner/repo slug from managed clone for {repo_name!r}; "
                "git remote.origin.url is missing or malformed"
            ),
        )

    container_name = await resolve_live_container(row.session_name)
    if container_name is None:
        return _failure_result(
            operation, session_name, repo_name,
            failure=FAILURE_NO_LIVE_CONTAINER,
            branch=row.branch,
            repo_slug=repo_slug,
            error_message=(
                f"no live container found for session {session_name!r}; "
                "expected a running docker container with that name"
            ),
        )

    try:
        gh_args = gh_args_for(row=row, repo_slug=repo_slug, mode=mode)
    except _OpArgsError as exc:
        return _failure_result(
            operation, session_name, repo_name,
            failure=exc.failure,
            branch=row.branch,
            repo_slug=repo_slug,
            container_name=container_name,
            error_message=str(exc),
        )

    cmd = ["docker", "exec", container_name, "gh", *gh_args]
    stdout, stderr, exit_code, timed_out = await run_cli(cmd, timeout=timeout)
    failure = classify_failure(stdout, stderr, exit_code, timed_out)

    error_message: str | None = None
    if failure is not None:
        error_message = _short_error(
            stdout, stderr,
            fallback={
                FAILURE_GH_MISSING: "gh CLI is not installed in the live container",
                FAILURE_AUTH_MISSING: "gh CLI is not authenticated to GitHub",
                FAILURE_TIMED_OUT: f"gh invocation timed out after {timeout}s",
                FAILURE_EXEC_FAILED: f"gh invocation failed (exit {exit_code})",
            }.get(failure, f"gh invocation failed: {failure}"),
        )

    return WorktreeGithubExecResult(
        operation=operation,
        session_name=session_name,
        repo_name=repo_name,
        ok=(failure is None),
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        timed_out=timed_out,
        container_name=container_name,
        branch=row.branch,
        repo_slug=repo_slug,
        command=cmd,
        failure=failure,
        error_message=error_message,
    )


class _OpArgsError(Exception):
    """Raised by an ops args factory when its inputs are invalid."""

    def __init__(self, failure: str, message: str) -> None:
        super().__init__(message)
        self.failure = failure


# ── Public API ────────────────────────────────────────────────────────


async def source_control_review_read_v1(
    session_name: str,
    repo_name: str,
    *,
    rows: list[WorktreeState],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> WorktreeGithubExecResult:
    """Read the current PR review state for the worktree's branch.

    Implementation of ``source_control.review.read`` for the
    ``autonomy/github`` capability. ``rows`` is the live worktree row
    list — typically supplied by the caller from
    :class:`tools.dashboard.worktree_monitor.WorktreeMonitor`. Maps
    internally to::

        docker exec <container> gh pr view <branch> --repo <owner>/<repo> --json <fields>
    """

    def _args(*, row, repo_slug, mode):  # noqa: ARG001 — fixed-shape factory
        return _pr_view_args(row.branch, repo_slug)

    return await _execute_op(
        operation=OP_REVIEW_READ,
        session_name=session_name,
        repo_name=repo_name,
        rows=rows,
        gh_args_for=_args,
        timeout=timeout,
    )


async def source_control_review_refresh_v1(
    session_name: str,
    repo_name: str,
    *,
    rows: list[WorktreeState],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> WorktreeGithubExecResult:
    """Re-fetch the PR review state for the worktree's branch.

    Same gh invocation as :func:`source_control_review_read_v1` (gh has
    no persistent client cache to invalidate), exposed as a separate op
    so callers can express intent — e.g. UI "Refresh" actions vs
    initial page-load reads.
    """

    def _args(*, row, repo_slug, mode):  # noqa: ARG001
        return _pr_view_args(row.branch, repo_slug)

    return await _execute_op(
        operation=OP_REVIEW_REFRESH,
        session_name=session_name,
        repo_name=repo_name,
        rows=rows,
        gh_args_for=_args,
        timeout=timeout,
    )


async def source_control_gates_watch_set_v1(
    session_name: str,
    repo_name: str,
    mode: str,
    *,
    rows: list[WorktreeState],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> WorktreeGithubExecResult:
    """Set the merge-gate watch mode for the worktree's repo.

    Implementation of ``source_control.gates.watch_set``. ``mode`` must
    be one of :data:`PR_WATCH_MODES` — ``"subscribed"``, ``"ignored"``,
    or ``"default"``. See :func:`_pr_watch_set_args` for the fixed gh
    template.
    """

    def _args(*, row, repo_slug, mode):  # noqa: ARG001
        if mode not in PR_WATCH_MODES:
            raise _OpArgsError(
                FAILURE_INVALID_MODE,
                f"invalid PR watch mode: {mode!r}; "
                f"expected one of {sorted(PR_WATCH_MODES)}",
            )
        return _pr_watch_set_args(repo_slug, mode)

    # Validate before resolution so a bad mode short-circuits without
    # touching docker — the caller likely passed a typo'd UI value.
    if mode not in PR_WATCH_MODES:
        return _failure_result(
            OP_GATES_WATCH_SET, session_name, repo_name,
            failure=FAILURE_INVALID_MODE,
            error_message=(
                f"invalid PR watch mode: {mode!r}; "
                f"expected one of {sorted(PR_WATCH_MODES)}"
            ),
        )

    return await _execute_op(
        operation=OP_GATES_WATCH_SET,
        session_name=session_name,
        repo_name=repo_name,
        rows=rows,
        gh_args_for=_args,
        timeout=timeout,
        mode=mode,
    )


__all__ = [
    "OP_REVIEW_READ",
    "OP_REVIEW_REFRESH",
    "OP_GATES_WATCH_SET",
    "PR_WATCH_SUBSCRIBED",
    "PR_WATCH_IGNORED",
    "PR_WATCH_DEFAULT",
    "PR_WATCH_MODES",
    "FAILURE_NO_LIVE_ROW",
    "FAILURE_NO_LIVE_CONTAINER",
    "FAILURE_NO_BRANCH",
    "FAILURE_NO_REPO_SLUG",
    "FAILURE_INVALID_MODE",
    "FAILURE_GH_MISSING",
    "FAILURE_AUTH_MISSING",
    "FAILURE_TIMED_OUT",
    "FAILURE_EXEC_FAILED",
    "PR_VIEW_FIELDS",
    "PR_LIST_FIELDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "WorktreeGithubExecResult",
    "CheckEntry",
    "ReviewPayload",
    "find_live_worktree_row",
    "resolve_live_container",
    "derive_repo_slug",
    "classify_failure",
    "run_cli",
    "normalize_review_payload",
    "normalize_review_stack",
    "source_control_review_read_v1",
    "source_control_review_refresh_v1",
    "source_control_gates_watch_set_v1",
]
