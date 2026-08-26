"""Destroy secret releases at their deadline, and reconcile across restarts.

The application-state half of secret delivery (auto-pw9bs.5). The delivery
layer writes a plaintext file into a session's ramfs subdirectory and
commits a durable record first (:mod:`tools.dashboard.dao.vault_releases`);
this module is what later destroys the file — at its time-to-live, at
session end, or on the first boot after a crash that outran the old
non-durable cleanup thread.

Two properties the design turns on (``graph://0c206bd8-1c6`` §4.2):

* **Destruction does not enter the container.** The secret lives in a host
  directory bind-mounted into the session, so the file is unlinked on the
  HOST path directly — no ``docker exec``. It works when the container has
  exited, is unresponsive, or was removed, which the acceptance pins.
  Unlinking releases the ramfs pages; they were never written anywhere
  else.

* **The durable record is the only cross-restart state.** ramfs survives a
  dashboard restart (only a host reboot wipes it), so a file written before
  a restart is still present when the dashboard returns. The record is what
  lets the returning process find it. Start-up reconciliation destroys the
  ones now overdue or whose session is gone, and reclaims session
  directories whose sessions no longer exist; the resumed periodic sweep
  destroys the rest at their deadlines.

``session_exists`` is injected rather than imported so the sweeper is a
pure function of (store, filesystem, clock, liveness) and the acceptance
can drive real restarts and real container removal without a live
dashboard.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from tools.dashboard.dao import vault_releases

logger = logging.getLogger(__name__)

#: How often the live sweep runs. A release is destroyed by its deadline
#: plus at most one interval — the acceptance bound.
SWEEP_INTERVAL_S = 30

#: Restart-race protection (host-ops finding, 2026-08-26): a stop-then-
#: relaunch briefly looks identical to an orphan — the old instance is gone
#: and the new instance's container/tmux is not up yet — and a sweep tick
#: landing in that gap was reclaiming a directory the new launch had just
#: re-provisioned, failing the launch closed a moment later. A per-session
#: directory younger than this many seconds is never reclaimed on the
#: "session_exists() is false" signal alone; the next tick catches a real
#: orphan once it has aged past the window.
RECLAIM_GRACE_S = 60


def _delivery_root() -> Path:
    """The host ramfs root sessions receive their subdirectories under
    (auto-pw9bs.4). Imported lazily: the constant lives in an agents-side
    module that pulls in storagekit, and the sweeper must import cleanly in
    a headless test that never mounts ramfs."""
    from agents.secret_ramfs import DELIVERY_MOUNT

    return Path(DELIVERY_MOUNT)


def _destroy_file(host_path: str) -> None:
    """Unlink one release file on the host. Missing is success — the file
    may already be gone (session-dir reclaimed, prior sweep, never
    materialised because the crash fell between record and delivery)."""
    try:
        Path(host_path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("vault sweep: could not unlink %s", host_path)
        raise


def _reclaim_session_dir(root: Path, session: str) -> bool:
    """Remove a whole per-session subdirectory, returning the ramfs pages of
    every file under it at once. Used at session end and for orphans."""
    target = root / session
    try:
        if target.is_dir():
            shutil.rmtree(target)
            return True
    except OSError:
        logger.exception("vault sweep: could not reclaim %s", target)
    return False


def sweep(
    *,
    session_exists: Callable[[str], bool],
    delivery_root: Path | None = None,
    now: int | None = None,
    overdue_reason: str = "expired",
    reclaim_grace_s: float = RECLAIM_GRACE_S,
) -> dict:
    """One sweep pass. Destroys, in the store's own outstanding order:

    * every release whose SESSION no longer exists — reason ``orphaned``;
    * every release now past its deadline — ``overdue_reason``.

    A still-valid release for a live session is LEFT for a later tick: the
    session is still entitled to it until its deadline. After the shreds,
    every per-session directory whose session no longer exists and has no
    remaining outstanding record is reclaimed whole — which also removes
    crash-residue directories that never had a record.

    Returns counts for logging: ``{shredded, reclaimed_dirs}``.
    """
    root = Path(delivery_root) if delivery_root is not None else _delivery_root()
    stamp = int(time.time() * 1000) if now is None else int(now)

    shredded = 0
    for rec in vault_releases.outstanding():
        session = rec["session"]
        gone = not session_exists(session)
        if gone:
            reason = "orphaned"
        elif stamp >= rec["expires_at"]:
            reason = overdue_reason
        else:
            continue
        _destroy_file(rec["host_path"])
        if vault_releases.mark_shredded(rec["id"], reason=reason, now=stamp):
            shredded += 1

    reclaimed = 0
    if root.is_dir():
        live = vault_releases.outstanding_sessions()
        for child in root.iterdir():
            if not child.is_dir():
                continue
            session = child.name
            if session in live or session_exists(session):
                continue
            try:
                age_s = time.time() - child.stat().st_mtime
            except OSError:
                continue  # vanished mid-scan: someone else reclaimed it
            if age_s < reclaim_grace_s:
                continue  # possibly a relaunch re-provisioning: not an orphan yet
            if _reclaim_session_dir(root, session):
                reclaimed += 1

    return {"shredded": shredded, "reclaimed_dirs": reclaimed}


def on_session_end(
    session: str,
    *,
    delivery_root: Path | None = None,
    now: int | None = None,
) -> dict:
    """The launcher's timely teardown hook (auto-pw9bs.5 / auto-f51kg).

    The launcher owns the session lifecycle and knows the instant a session
    ends, so it — not the periodic sweep — is what returns the memory
    promptly: it calls this, which marks the session's still-outstanding
    releases shredded with reason ``session_end`` and reclaims the whole
    per-session subdirectory at once. The sweeper remains the BACKSTOP for
    the case this call never happens (the launcher died, the dashboard
    restarted): its gone-session pass marks the same releases ``orphaned``
    and reclaims the directory then instead.

    Idempotent and safe to race with the sweeper: reclaiming an
    already-removed directory is a no-op, and ``mark_shredded`` keeps the
    first reason, so whichever path fires first wins and the second is
    inert. Returns ``{shredded, reclaimed_dir}``."""
    root = Path(delivery_root) if delivery_root is not None else _delivery_root()
    stamp = int(time.time() * 1000) if now is None else int(now)
    shredded = 0
    for rec in vault_releases.outstanding():
        if rec["session"] != session:
            continue
        _destroy_file(rec["host_path"])
        if vault_releases.mark_shredded(rec["id"], reason="session_end",
                                        now=stamp):
            shredded += 1
    reclaimed = _reclaim_session_dir(root, session)
    return {"shredded": shredded, "reclaimed_dir": reclaimed}


def reconcile_on_startup(
    *,
    session_exists: Callable[[str], bool],
    delivery_root: Path | None = None,
    now: int | None = None,
    reclaim_grace_s: float = RECLAIM_GRACE_S,
) -> dict:
    """The first pass after a (re)start. Same core as :func:`sweep`, but a
    release found already past its deadline is shredded with reason
    ``reconciled`` rather than ``expired`` — the audit then distinguishes a
    release the live sweeper caught at its deadline from one that outlived a
    downtime and was caught on the way back up. Everything still valid and
    live is left for the resumed periodic sweep, which destroys it at its
    own deadline."""
    result = sweep(
        session_exists=session_exists,
        delivery_root=delivery_root,
        now=now,
        overdue_reason="reconciled",
        reclaim_grace_s=reclaim_grace_s,
    )
    logger.info(
        "vault release reconciliation: %d destroyed, %d directories reclaimed",
        result["shredded"], result["reclaimed_dirs"],
    )
    return result
