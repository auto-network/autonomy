"""One-click software update: follow the GitHub origin by fast-forward.

A fleet node runs its dashboard straight out of the checkout at ``_REPO_ROOT``
(the container's ``/app`` bind-mount), and the dashboard hot-reloads code on
file change. So "update the software" is exactly ``git fetch origin`` followed
by ``git reset --hard`` onto the tracked origin branch — the reloader picks the
new code up on its own. This module is the whole mechanism: the git math that
tells a machine how far behind origin it is, and the guarded fast-forward that
performs the update.

The author machine is protected structurally, not by a flag. The desktop where
commits are authored is always AHEAD of origin (it holds commits origin has
not received yet), and a hard reset there would destroy that unpushed work. So
``can_update`` requires ``ahead == 0`` — a pure fast-forward — which no author
machine ever satisfies. A follower node is never ahead, only behind, so it only
ever fast-forwards. The "which machines follow origin" question answers itself
from commit history; there is nothing to configure on a node (the zero-config
deploy goal), and a machine cannot reset away work it has not shipped.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The remote we follow. Already configured on host and node
# (git@github-autonomy:auto-network/autonomy.git).
_ORIGIN = "origin"

# git commands are bounded so a network or lock hiccup can never wedge the
# request thread. The fetch reaches GitHub, so it gets a real network budget;
# the local rev-list/status calls are near-instant.
_LOCAL_TIMEOUT_S = 5
_FETCH_TIMEOUT_S = 45

# Non-interactive SSH: never prompt for a passphrase or host key, and fail fast
# instead of hanging the fetch when the key or network is unavailable.
_GIT_ENV = {
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
    "GIT_TERMINAL_PROMPT": "0",
}


class SoftwareUpdateError(RuntimeError):
    """A git step failed in a way that blocks the update."""


def _git(*args: str, timeout: int = _LOCAL_TIMEOUT_S, check: bool = True) -> str:
    import os

    result = subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **_GIT_ENV},
    )
    if check and result.returncode != 0:
        raise SoftwareUpdateError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _tracked_branch() -> str:
    """This checkout's branch (the origin ref we compare against is
    ``origin/<branch>``). Falls back to ``master`` for a detached HEAD."""
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    except (SoftwareUpdateError, subprocess.SubprocessError, OSError):
        return "master"
    return branch if branch and branch != "HEAD" else "master"


def _short(sha: str) -> str:
    return sha[:10]


def _commit_list(rev_range: str, limit: int = 25) -> list[dict[str, str]]:
    """`{sha, subject}` for each commit in ``rev_range`` (newest first)."""
    out = _git(
        "log", f"--max-count={limit}", "--format=%h%x00%s", rev_range,
        check=False,
    )
    commits: list[dict[str, str]] = []
    for line in out.splitlines():
        if "\x00" in line:
            sha, subject = line.split("\x00", 1)
            commits.append({"sha": sha, "subject": subject})
    return commits


def update_status(*, fetch: bool = True) -> dict[str, Any]:
    """How this machine stands relative to origin.

    ``fetch=True`` refreshes the origin ref first (the accurate answer needs a
    network round-trip); pass ``fetch=False`` for a cheap cached read. Any git
    failure degrades to a status carrying ``error`` rather than raising, so the
    profile dropdown can always render *something*.
    """
    branch = _tracked_branch()
    origin_ref = f"{_ORIGIN}/{branch}"
    status: dict[str, Any] = {
        "branch": branch,
        "origin_ref": origin_ref,
        "fetched": False,
        "error": None,
    }
    if fetch:
        try:
            _git("fetch", _ORIGIN, branch, timeout=_FETCH_TIMEOUT_S)
            status["fetched"] = True
        except (SoftwareUpdateError, subprocess.SubprocessError, OSError) as exc:
            # Fall through to a cached read: a stale behind-count still beats a
            # blank panel, and the caller sees why the fetch didn't land.
            status["error"] = f"fetch failed: {exc}"

    try:
        current = _git("rev-parse", "HEAD").strip()
        current_subject = _git("log", "-1", "--format=%s").strip()
        # An absent origin ref (never fetched, or a fresh clone) is not an error
        # — it just means "nothing to compare against yet".
        origin_present = _git(
            "rev-parse", "--verify", "--quiet", origin_ref, check=False,
        ).strip()
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no").strip())
    except (SoftwareUpdateError, subprocess.SubprocessError, OSError) as exc:
        status["error"] = status["error"] or str(exc)
        status.update(current=None, ahead=0, behind=0, dirty=False,
                      can_update=False, mode="unknown")
        return status

    status.update(
        current=_short(current),
        current_subject=current_subject,
        dirty=dirty,
    )

    if not origin_present:
        status.update(ahead=0, behind=0, can_update=False, mode="unknown",
                      origin_commit=None)
        return status

    ahead = int(_git("rev-list", "--count", f"{origin_ref}..HEAD").strip() or "0")
    behind = int(_git("rev-list", "--count", f"HEAD..{origin_ref}").strip() or "0")
    # Does the update touch deploy/ (entrypoint, compose, Dockerfile)? Those
    # need a container recreate, not just a code hot-reload — surface it so the
    # UI can tell the operator a plain update won't fully apply.
    deploy_changed = bool(_git(
        "diff", "--name-only", f"HEAD..{origin_ref}", "--", "deploy/",
        check=False,
    ).strip())

    status.update(
        origin_commit=_short(origin_present),
        origin_subject=(_commit_list(f"HEAD..{origin_ref}", limit=1)[:1] or
                        [{"subject": ""}])[0].get("subject", ""),
        ahead=ahead,
        behind=behind,
        # A pure fast-forward: behind, not ahead, clean tree. An author machine
        # (ahead > 0) never qualifies — the guardrail is git history itself.
        can_update=bool(behind > 0 and ahead == 0 and not dirty),
        mode="author" if ahead > 0 else "follower",
        incoming=_commit_list(f"HEAD..{origin_ref}"),
        deploy_changed=deploy_changed,
    )
    return status


def perform_update() -> dict[str, Any]:
    """Fast-forward this checkout onto origin. Re-verifies the guards under a
    fresh fetch, then ``reset --hard``. Refuses anything that is not a clean
    fast-forward so it can never destroy unshipped local work.

    Returns the applied range for the UI. The dashboard hot-reloads the new
    code on its own; ``deploy_changed`` tells the caller a container recreate is
    still needed for entrypoint/compose changes.
    """
    status = update_status(fetch=True)
    if status.get("error") and not status.get("fetched"):
        raise SoftwareUpdateError(status["error"])
    if status.get("dirty"):
        raise SoftwareUpdateError(
            "working tree has uncommitted changes; refusing to reset")
    if status.get("mode") == "author" or status.get("ahead", 0) > 0:
        raise SoftwareUpdateError(
            f"this machine is {status.get('ahead', 0)} commit(s) ahead of "
            f"{status['origin_ref']} (an author machine); refusing hard reset")
    if status.get("behind", 0) == 0:
        return {
            "updated": False,
            "reason": "already up to date",
            "from": status.get("current"),
            "to": status.get("current"),
            "count": 0,
            "commits": [],
            "deploy_changed": False,
        }

    before = _git("rev-parse", "HEAD").strip()
    incoming = status.get("incoming", [])
    deploy_changed = status.get("deploy_changed", False)
    _git("reset", "--hard", status["origin_ref"])
    after = _git("rev-parse", "HEAD").strip()
    return {
        "updated": True,
        "from": _short(before),
        "to": _short(after),
        "count": status.get("behind", 0),
        "commits": incoming,
        "deploy_changed": deploy_changed,
    }
