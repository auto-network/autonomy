"""The git commit a running process actually loaded, captured once.

``git rev-parse HEAD`` run fresh always answers "what's on disk right now",
not "what did this process import". A ``--reload`` worker or a connector
subprocess can keep running old code for hours after disk moves on (this
cost a real fleet-sync debugging cycle on 2026-08-23 -- see fleet_doctor's
STALE-CODE check). Importing this module captures the commit at that
moment, in that process, once -- so a live-process query can be compared
against the current disk HEAD to tell the two apart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git_rev(ref: str = "HEAD") -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", ref],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


#: Captured once, at import time, in this process. Compare against
#: disk_head() to detect a process still running pre-deploy code.
PROCESS_COMMIT: str | None = _git_rev("HEAD")


def disk_head() -> str | None:
    """The commit checked out right now -- a fresh call, not cached."""
    return _git_rev("HEAD")


def is_stale(*, disk_commit: str | None = None) -> bool | None:
    """True if this process's loaded code predates what's on disk now.

    None (unknown) rather than False when either commit couldn't be
    resolved -- an unknown answer must never read as "confirmed fresh"."""
    disk = disk_commit if disk_commit is not None else disk_head()
    if PROCESS_COMMIT is None or disk is None:
        return None
    return PROCESS_COMMIT != disk
