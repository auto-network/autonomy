"""worktree_cmd.py — graph worktree subcommand: list and prune session worktrees.

Thin CLI wrapper around :mod:`agents.workspace_manager` cleanup helpers. Useful
for manually cleaning the ``data/worktrees/`` directory when the dashboard's
periodic pruner hasn't caught up (or isn't running).
"""

from __future__ import annotations

import sys
from pathlib import Path

from agents.workspace_manager import (
    WORKTREES_DIR,
    CleanupResult,
    cleanup_session_worktrees,
    prune_orphan_worktrees,
)


def _get_live_session_names() -> list[str]:
    """Query dashboard.db for live tmux session names.

    Returns empty list if the DB is unavailable (missing file, import error).
    Callers should treat this as "unknown live set" and refuse to prune
    unless ``--force-all`` is passed.
    """
    try:
        from tools.dashboard.dao.dashboard_db import get_live_sessions
        return [r["tmux_name"] for r in get_live_sessions()]
    except Exception:  # pragma: no cover — DB not provisioned in most CLI runs
        return []


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
    live = set(_get_live_session_names())
    entries = sorted(p for p in worktrees_dir.iterdir() if p.is_dir())
    if not entries:
        print(f"(empty) {worktrees_dir}")
        return
    print(f"Worktrees under {worktrees_dir}:")
    for entry in entries:
        status = "LIVE  " if entry.name in live else "orphan"
        repos = sorted(
            p.name for p in entry.iterdir() if p.is_dir()
        ) if entry.is_dir() else []
        print(f"  [{status}] {entry.name}  repos={len(repos)}  {repos}")


def cmd_worktree_prune(args) -> None:
    """Prune worktrees that don't belong to any live session.

    With ``--session NAME``, only clean that session's worktrees.
    With ``--force``, also clean worktrees that have uncommitted changes
    or unpushed commits (otherwise those are preserved with a warning).
    """
    worktrees_dir = Path(args.worktrees_dir) if args.worktrees_dir else WORKTREES_DIR
    force = bool(args.force)

    if args.session:
        result = cleanup_session_worktrees(
            args.session, force=force, worktrees_dir=worktrees_dir,
        )
        _print_result(args.session, result)
        if result.errors:
            sys.exit(1)
        return

    live = _get_live_session_names()
    if not live and not args.force_all:
        print(
            "ERROR: cannot determine live sessions (dashboard.db unavailable).\n"
            "Pass --force-all to prune ALL worktrees regardless of liveness, "
            "or --session NAME to target one.",
            file=sys.stderr,
        )
        sys.exit(2)

    live_names = live if not args.force_all else []
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
