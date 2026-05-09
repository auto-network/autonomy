"""Shared git status helpers for dispatcher and workspace cleanup paths."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_status_porcelain_lines(
    repo: Path,
    *,
    untracked: str = "normal",
    timeout: int = 15,
) -> tuple[int, list[str]]:
    """Return ``(returncode, non-empty porcelain lines)`` for ``repo``.

    ``untracked`` mirrors git's ``--untracked-files`` modes: ``normal``,
    ``all``, or ``no``. Callers decide whether a non-zero return code should
    be treated as clean, dirty, or an exceptional path.
    """
    if untracked not in {"normal", "all", "no"}:
        raise ValueError(f"unsupported untracked mode: {untracked!r}")
    args = ["git", "status", "--porcelain"]
    if untracked != "normal":
        args.append(f"--untracked-files={untracked}")
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(repo),
    )
    lines = [line for line in result.stdout.splitlines() if line]
    return result.returncode, lines


def summarize_git_status_lines(lines: list[str], *, max_entries: int = 10) -> str:
    """Format porcelain lines into the dispatcher's compact summary string."""
    if not lines:
        return ""
    summary = "; ".join(lines[:max_entries])
    if len(lines) > max_entries:
        summary += f" ... and {len(lines) - max_entries} more"
    return summary


def working_tree_clean_and_summary(
    repo: Path,
    *,
    untracked: str = "normal",
    timeout: int = 10,
) -> tuple[bool, str]:
    """Return ``(is_clean, summary)`` for a repo working tree.

    This intentionally preserves the dispatcher's historical behavior: only
    the porcelain output drives cleanliness, so callers that were already
    treating an empty stdout as clean keep doing so even if ``git status``
    exits non-zero.
    """
    _rc, lines = git_status_porcelain_lines(repo, untracked=untracked, timeout=timeout)
    if not lines:
        return True, ""
    return False, summarize_git_status_lines(lines)


def has_working_tree_changes(
    repo: Path,
    *,
    untracked: str = "normal",
    timeout: int = 15,
    error_is_dirty: bool = True,
) -> bool:
    """Return True if ``git status --porcelain`` reports any changes."""
    rc, lines = git_status_porcelain_lines(repo, untracked=untracked, timeout=timeout)
    if rc != 0:
        return error_is_dirty
    return bool(lines)
