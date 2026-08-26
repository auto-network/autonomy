"""Session worktree status, synchronization, merge, and host maintenance.

``host-prune`` is a manual host escape hatch. Dashboard lifecycle cleanup calls
the workspace-manager functions directly; no session or daemon shells out to
this CLI verb.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from agents.workspace_manager import (
    WORKTREES_DIR,
    CleanupResult,
    cleanup_session_worktrees,
    prune_orphan_worktrees,
)


class WorktreeCommandError(RuntimeError):
    """A user-facing worktree command failure."""


@dataclass(frozen=True)
class WorktreeTarget:
    root: Path
    session_name: str
    repo_name: str
    row: dict


def _api_request(method: str, path: str) -> object:
    """Call the authenticated dashboard API from a session container."""
    base = os.environ.get("GRAPH_API", "https://localhost:8080").rstrip("/")
    token = os.environ.get("CROSSTALK_TOKEN", "").strip()
    if not token:
        raise WorktreeCommandError(
            "worktree status/sync/merge require this session's CROSSTALK_TOKEN"
        )
    request = urllib.request.Request(
        f"{base}{path}",
        data=b"{}" if method != "GET" else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=600, context=context) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {}
        message = body.get("message") or body.get("error") or str(exc)
        raise WorktreeCommandError(message) from None
    except urllib.error.URLError as exc:
        raise WorktreeCommandError(f"cannot reach dashboard: {exc.reason}") from None
    return json.loads(raw) if raw else None


def _git(path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "git command failed").strip()
        raise WorktreeCommandError(detail) from None
    return result.stdout.strip()


def _resolve_target(path_value: str | None) -> WorktreeTarget:
    """Resolve a dashboard row from a local path's Git worktree identity."""
    requested = Path(path_value or ".").expanduser().resolve()
    root = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve()
    branch = _git(root, "branch", "--show-current")
    common_raw = Path(_git(root, "rev-parse", "--git-common-dir"))
    common = (root / common_raw).resolve() if not common_raw.is_absolute() else common_raw.resolve()

    session_name = os.environ.get("AUTONOMY_SESSION", "").strip()
    if not session_name and branch.startswith("session/"):
        session_name = branch.removeprefix("session/")
    if not session_name:
        raise WorktreeCommandError(
            "cannot determine the session for this worktree; AUTONOMY_SESSION is unset"
        )

    rows = _api_request("GET", "/api/worktrees")
    if not isinstance(rows, list):
        raise WorktreeCommandError("dashboard returned an invalid worktree list")
    candidates = [row for row in rows if row.get("session_name") == session_name]
    if not candidates:
        raise WorktreeCommandError(f"dashboard has no worktree for session {session_name}")

    matching: list[dict] = []
    for row in candidates:
        clone_value = row.get("managed_clone")
        if not clone_value:
            continue
        clone = Path(str(clone_value)).resolve()
        if common in (clone, clone / ".git"):
            matching.append(row)
    if not matching:
        matching = [row for row in candidates if row.get("branch") == branch]
    if len(matching) != 1:
        repos = ", ".join(sorted(str(row.get("repo_name")) for row in candidates))
        raise WorktreeCommandError(
            f"{requested} does not identify exactly one session worktree; candidates: {repos}"
        )
    row = matching[0]
    return WorktreeTarget(root, session_name, str(row["repo_name"]), row)


def _target_path(target: WorktreeTarget, suffix: str) -> str:
    session = urllib.parse.quote(target.session_name, safe="")
    repo = urllib.parse.quote(target.repo_name, safe="")
    return f"/api/worktrees/{session}/{repo}/{suffix}"


def _refresh(target: WorktreeTarget) -> dict:
    row = _api_request("POST", _target_path(target, "refresh"))
    if not isinstance(row, dict):
        raise WorktreeCommandError("dashboard returned an invalid worktree state")
    return row


def _readiness(row: dict) -> str:
    if row.get("clone_stale"):
        return "needs base sync"
    if row.get("rebase_required"):
        return "needs rebase"
    if row.get("ff_eligible"):
        return "ready"
    if not row.get("commits_ahead") and not row.get("is_dirty"):
        return "nothing to merge"
    if row.get("is_dirty") and not row.get("commits_ahead"):
        return "uncommitted changes only"
    return "not fast-forward eligible"


def _print_status(target: WorktreeTarget, row: dict) -> None:
    dirty = f"dirty ({row.get('dirty_count', 0)} paths)" if row.get("is_dirty") else "clean"
    print(f"Worktree: {target.root}")
    print(f"Session:  {target.session_name}")
    print(f"Repo:     {target.repo_name}")
    print(f"Branch:   {row.get('branch') or '?'} -> {row.get('target_branch') or '?'}")
    print(f"State:    {dirty}; {row.get('commits_ahead', 0)} commit(s) ahead")
    print(f"Base:     {'stale' if row.get('clone_stale') else 'synchronized'}")
    print(f"Merge:    {_readiness(row)}")


def _sync_and_rebase(target: WorktreeTarget) -> dict:
    response = _api_request("POST", _target_path(target, "sync-base"))
    if not isinstance(response, dict) or not isinstance(response.get("state"), dict):
        raise WorktreeCommandError("dashboard returned an invalid sync result")
    row = response["state"]
    if row.get("rebase_required"):
        if _git(target.root, "status", "--porcelain"):
            raise WorktreeCommandError(
                "base advanced, but this worktree is dirty; commit or stash before rebasing"
            )
        target_branch = str(row.get("target_branch") or "").strip()
        if not target_branch:
            raise WorktreeCommandError("dashboard did not report a target branch")
        _git(target.root, "rebase", target_branch)
        row = _refresh(target)
    return row


def _run_session_command(args, action) -> None:
    try:
        action(_resolve_target(getattr(args, "path", None)))
    except WorktreeCommandError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def cmd_worktree_status(args) -> None:
    def _action(target: WorktreeTarget) -> None:
        _print_status(target, _refresh(target))

    _run_session_command(args, _action)


def cmd_worktree_sync(args) -> None:
    def _action(target: WorktreeTarget) -> None:
        row = _sync_and_rebase(target)
        _print_status(target, row)

    _run_session_command(args, _action)


def cmd_worktree_merge(args) -> None:
    def _action(target: WorktreeTarget) -> None:
        row = _sync_and_rebase(target)
        if row.get("is_dirty"):
            raise WorktreeCommandError(
                "worktree is dirty; commit or stash before merging the branch"
            )
        if not row.get("commits_ahead"):
            print("Nothing to merge; this branch is already on the target.")
            return
        if not row.get("ff_eligible"):
            raise WorktreeCommandError(f"worktree is {_readiness(row)}")
        result = _api_request("POST", _target_path(target, "merge"))
        if not isinstance(result, dict) or not result.get("ok"):
            raise WorktreeCommandError("dashboard returned an invalid merge result")
        commit = result.get("commit") or ""
        message = result.get("message") or "merged"
        print(f"Merged {target.session_name}/{target.repo_name} at {commit}: {message}")

    _run_session_command(args, _action)


def _get_live_session_names() -> list[str] | None:
    """Query dashboard.db for live tmux session names.

    Returns ``None`` if the DB is unavailable (missing file, import error).
    Cleanup callers must distinguish that from a known-empty live set.
    """
    try:
        from tools.dashboard.dao.dashboard_db import get_live_sessions
        return [r["tmux_name"] for r in get_live_sessions()]
    except Exception:  # pragma: no cover — DB not provisioned in most CLI runs
        return None


def _print_result(name: str, result: CleanupResult) -> None:
    if result.removed:
        print(f"  {name}: removed {len(result.removed)}")
        for path in result.removed:
            print(f"    - {path}")
    for path, reason in result.preserved:
        print(f"  {name}: PRESERVED  {path}  ({reason})")
    for path, err in result.errors:
        print(f"  {name}: ERROR      {path}  ({err})", file=sys.stderr)


def cmd_worktree_list(args) -> None:
    """List session worktrees under ``data/worktrees/``."""
    worktrees_dir = Path(args.worktrees_dir) if args.worktrees_dir else WORKTREES_DIR
    if not worktrees_dir.exists():
        print(f"No worktrees dir at {worktrees_dir}")
        return
    live_names = _get_live_session_names()
    live = set(live_names or [])
    entries = sorted(p for p in worktrees_dir.iterdir() if p.is_dir())
    if not entries:
        print(f"(empty) {worktrees_dir}")
        return
    print(f"Worktrees under {worktrees_dir}:")
    for entry in entries:
        if live_names is None:
            status = "unknown"
        else:
            status = "LIVE  " if entry.name in live else "orphan"
        repos = sorted(
            p.name for p in entry.iterdir() if p.is_dir()
        ) if entry.is_dir() else []
        print(f"  [{status}] {entry.name}  repos={len(repos)}  {repos}")


def cmd_worktree_host_prune(args) -> None:
    """Prune worktrees that don't belong to any live session.

    With ``--session NAME``, only clean that session's worktrees.
    With ``--force``, also clean worktrees that have uncommitted changes
    or local commits (otherwise those are preserved with a warning).
    """
    session_env = next(
        (
            name
            for name in ("AUTONOMY_SESSION", "GRAPH_SESSION")
            if os.environ.get(name, "").strip()
        ),
        None,
    )
    bd_actor = os.environ.get("BD_ACTOR", "").strip()
    if not session_env and ":" in bd_actor:
        session_env = bd_actor.split(":", 1)[1]
    if session_env or os.environ.get("CROSSTALK_TOKEN", "").strip():
        print(
            "ERROR: graph worktree host-prune cannot run inside a session; "
            "session worktree cleanup is owned by the host lifecycle.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    worktrees_dir = Path(args.worktrees_dir) if args.worktrees_dir else WORKTREES_DIR
    force = bool(args.force)

    if args.session:
        live_names = _get_live_session_names()
        if live_names is None:
            print(
                "ERROR: cannot verify that the target session has ended; "
                "dashboard.db is unavailable",
                file=sys.stderr,
            )
            raise SystemExit(2)
        live = set(live_names)
        if args.session in live:
            print(
                f"ERROR: refusing to prune live session {args.session}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        result = cleanup_session_worktrees(
            args.session, force=force, worktrees_dir=worktrees_dir,
        )
        _print_result(args.session, result)
        if result.errors:
            sys.exit(1)
        return

    live = _get_live_session_names()
    if live is None and not args.force_all:
        print(
            "ERROR: cannot determine live sessions (dashboard.db unavailable).\n"
            "Pass --force-all to prune ALL worktrees regardless of liveness, "
            "or --session NAME to target one.",
            file=sys.stderr,
        )
        sys.exit(2)

    live_names = (live or []) if not args.force_all else []
    results = prune_orphan_worktrees(
        live_names, force=force, worktrees_dir=worktrees_dir,
    )
    if not results:
        print("No orphan worktrees found.")
        return
    total_removed = 0
    total_preserved = 0
    total_errors = 0
    for name, result in results.items():
        _print_result(name, result)
        total_removed += len(result.removed)
        total_preserved += len(result.preserved)
        total_errors += len(result.errors)
    print(
        f"\nSummary: {total_removed} removed, {total_preserved} preserved, "
        f"{total_errors} errors across {len(results)} session(s)."
    )
    if total_errors:
        sys.exit(1)


def cmd_worktree_default(args) -> None:
    """Default handler for ``graph worktree`` with no action — list."""
    cmd_worktree_list(args)
