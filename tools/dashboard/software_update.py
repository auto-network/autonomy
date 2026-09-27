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

import asyncio
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The remote we follow, and the branch on it. A follower tracks the branch the
# author PUSHES (origin/master) regardless of what it named its own local
# checkout — a node may check its release branch out as "shipped", and deriving
# the ref from the local branch name fetches an origin ref that does not exist.
# The origin is the PUBLIC repo over HTTPS, so a follower pulls with no key and
# no credentials (the SSH remote is only the author machine's push path).
_ORIGIN = "origin"
_TRACK_BRANCH = "master"
_PUBLIC_ORIGIN_URL = "https://github.com/auto-network/autonomy.git"

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


def _ensure_origin() -> None:
    """A node image keeps the public origin (deploy/Dockerfile); a code volume
    seeded before that has none. Add it, so such a node self-heals on its
    first check instead of never seeing an update."""
    if _git("remote", "get-url", _ORIGIN, check=False).strip():
        return
    _git("remote", "add", _ORIGIN, _PUBLIC_ORIGIN_URL)
    logger.info("software_update: added missing origin %s", _PUBLIC_ORIGIN_URL)


def _checked_at() -> str | None:
    """When origin was last fetched: git's own FETCH_HEAD mtime, so the answer
    survives restarts and needs no bookkeeping of ours."""
    try:
        git_dir = _git("rev-parse", "--absolute-git-dir").strip()
        mtime = os.path.getmtime(os.path.join(git_dir, "FETCH_HEAD"))
    except (SoftwareUpdateError, subprocess.SubprocessError, OSError):
        return None
    return datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_update_path() -> Path:
    from tools.data_paths import DATA_ROOT

    return Path(DATA_ROOT) / "software-update" / "last-update.json"


def last_update() -> dict[str, Any] | None:
    """The most recent applied update on this machine, or None."""
    try:
        return json.loads(_last_update_path().read_text())
    except (OSError, ValueError):
        return None


def _record_update(record: dict[str, Any]) -> None:
    path = _last_update_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record))
        tmp.replace(path)
    except OSError:
        logger.warning("software_update: could not record the applied update", exc_info=True)


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
    origin_ref = f"{_ORIGIN}/{_TRACK_BRANCH}"
    status: dict[str, Any] = {
        "track_branch": _TRACK_BRANCH,
        "origin_ref": origin_ref,
        "fetched": False,
        "error": None,
    }
    if fetch:
        try:
            _ensure_origin()
            _git("fetch", _ORIGIN, _TRACK_BRANCH, timeout=_FETCH_TIMEOUT_S)
            status["fetched"] = True
        except (SoftwareUpdateError, subprocess.SubprocessError, OSError) as exc:
            # Fall through to a cached read: a stale behind-count still beats a
            # blank panel, and the caller sees why the fetch didn't land.
            status["error"] = f"fetch failed: {exc}"

    status["checked_at"] = _checked_at()
    status["last_update"] = last_update()
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


def perform_update(*, automatic: bool = False) -> dict[str, Any]:
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
    _record_update({
        "automatic": automatic,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "from": _short(before),
        "to": _short(after),
        "count": status.get("behind", 0),
    })
    return {
        "updated": True,
        "from": _short(before),
        "to": _short(after),
        "count": status.get("behind", 0),
        "commits": incoming,
        "deploy_changed": deploy_changed,
    }


#: How often the poller re-reads the preference. Checks themselves run every
#: ``interval_minutes``; this only bounds how soon a changed preference acts.
POLL_TICK_S = 60


async def run_poller(
    publish: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    read_preference: Callable[[], dict[str, Any]] | None = None,
    status_fn: Callable[..., dict[str, Any]] = update_status,
    update_fn: Callable[..., dict[str, Any]] = perform_update,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    tick_s: float = POLL_TICK_S,
) -> None:
    """The scheduled check (graph://89d3c8df-544 §6, driver S7).

    While ``auto_check`` is on, fetch origin once per ``interval_minutes``;
    with ``auto_install`` also on, apply an available fast-forward right away
    (no idle check: the dashboard hot-reloads and sessions keep running).
    ``publish`` receives ``{behind, can_update, ...}`` when ``behind`` changes
    and after an automatic install. With ``auto_check`` off nothing is fetched.
    """
    if read_preference is None:
        from tools.dashboard.software_update_settings import read_preference as _read

        read_preference = _read
    last_fetch: float | None = None
    last_behind: int | None = None
    while True:
        try:
            pref = await asyncio.to_thread(read_preference)
            interval_s = max(int(pref.get("interval_minutes") or 360), 30) * 60
            if pref.get("auto_check") and (last_fetch is None or clock() - last_fetch >= interval_s):
                last_fetch = clock()
                status = await asyncio.to_thread(status_fn, fetch=True)
                if pref.get("auto_install") and status.get("can_update"):
                    try:
                        result = await asyncio.to_thread(update_fn, automatic=True)
                    except SoftwareUpdateError as exc:
                        logger.warning("software_update: automatic install refused: %s", exc)
                    else:
                        logger.info(
                            "software_update: installed automatically %s -> %s (%s commits)",
                            result.get("from"), result.get("to"), result.get("count"),
                        )
                        status = await asyncio.to_thread(status_fn, fetch=False)
                        last_behind = status.get("behind")
                        await publish({
                            "behind": status.get("behind"), "can_update": status.get("can_update"),
                            "installed": result,
                        })
                        await sleep(tick_s)
                        continue
                behind = status.get("behind")
                if behind != last_behind:
                    last_behind = behind
                    await publish({"behind": behind, "can_update": status.get("can_update")})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("software_update: poller tick failed")
        await sleep(tick_s)
