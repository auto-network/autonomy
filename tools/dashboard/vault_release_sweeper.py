"""Close the ledger on ended releases, and reconcile across restarts.

The application-state half of secret delivery (auto-pw9bs.5), reduced to
BOOKKEEPING on 2026-08-30: a delivered secret now lives in the requesting
container's own PRIVATE mount-namespace ramfs, which the kernel frees the
instant the container exits. There is no shared host directory, so there is
nothing on disk for this module to destroy — and therefore nothing it can
destroy wrongly.

That reduction is the fix for a four-incident class (Aug 23/27/28/30): the
old design kept every session's delivery directory in ONE shared host ramfs
that every dashboard on the daemon could see and sweep, each trusting its
own machine-local tmux for liveness — so any sibling dashboard (another
node, a stray worker) reclaimed every live session's secrets at the first
tick past the grace window, silently. The record of that lesson lives here
so the shared-root design does not come back: destruction of another
process's delivery must be STRUCTURALLY impossible, not carefully avoided.

What remains is the value-free lease ledger: rows must not stay
``outstanding`` forever once their session is gone. The sweep marks them
``orphaned`` (or ``session_end`` via the launcher's hook) — a pure record
transition. Legacy rows written by the retired shared-root design may still
name a real host path; those get a best-effort unlink on close, which
touches only paths this node's own ledger recorded.

``session_exists`` is injected rather than imported so the sweeper is a
pure function of (store, clock, liveness). ``None`` means the caller could
not answer liveness this tick; rows are then left outstanding — a stale
ledger row is noise, but a wrong transition is a lie in the audit.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from tools.dashboard.dao import vault_releases

logger = logging.getLogger(__name__)

#: How often the live sweep runs.
SWEEP_INTERVAL_S = 30

#: Prefix of the RETIRED shared delivery root. A lease's ``host_path`` under
#: it is a real on-disk location from the old design and gets a best-effort
#: unlink when the lease closes; the ``container-ns:`` locators the current
#: design records are not paths and are never touched.
_LEGACY_HOST_PREFIX = "/run/autonomy-secrets/"


def _destroy_legacy_file(host_path: str) -> None:
    """Unlink a legacy shared-root file, if the lease recorded one. Missing
    is success; the container-ns locator of the current design is skipped."""
    if not host_path.startswith(_LEGACY_HOST_PREFIX):
        return
    try:
        Path(host_path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("vault sweep: could not unlink legacy %s", host_path)


def sweep(
    *,
    session_exists: "Callable[[str], bool] | None",
    now: int | None = None,
    overdue_reason: str = "expired",
) -> dict:
    """One bookkeeping pass over the outstanding leases.

    * a release whose SESSION no longer exists closes as ``orphaned``;
    * a legacy release past its deadline closes as *overdue_reason*
      (current releases carry no deadline — session lifetime).

    ``session_exists=None`` means liveness could not be answered this tick
    (the caller's probe failed). An ambiguous answer never drives a
    transition — the 2026-04-20 rule — so those rows simply wait.

    Returns ``{closed}`` for logging.
    """
    stamp = int(time.time() * 1000) if now is None else int(now)

    closed = 0
    for rec in vault_releases.outstanding():
        session = rec["session"]
        deadline = rec.get("expires_at")
        gone = session_exists is not None and not session_exists(session)
        if gone:
            reason = "orphaned"
        elif deadline is not None and stamp >= deadline:
            reason = overdue_reason
        else:
            continue
        _destroy_legacy_file(rec["host_path"])
        if vault_releases.mark_shredded(rec["id"], reason=reason, now=stamp):
            closed += 1
            logger.info(
                "vault sweep: closed release %s (session=%s reason=%s)",
                rec["id"], session, reason,
            )
    return {"closed": closed}


def on_session_end(
    session: str,
    *,
    now: int | None = None,
) -> dict:
    """The launcher's timely session-end hook: close the session's
    outstanding leases as ``session_end``. The delivered files themselves
    died with the container's private mount; only legacy shared-root paths
    (if any lease still names one) get a best-effort unlink. Idempotent and
    safe to race with the sweep: ``mark_shredded`` keeps the first reason."""
    stamp = int(time.time() * 1000) if now is None else int(now)
    closed = 0
    for rec in vault_releases.outstanding():
        if rec["session"] != session:
            continue
        _destroy_legacy_file(rec["host_path"])
        if vault_releases.mark_shredded(rec["id"], reason="session_end",
                                        now=stamp):
            closed += 1
    return {"closed": closed}


def reconcile_on_startup(
    *,
    session_exists: "Callable[[str], bool] | None",
    now: int | None = None,
) -> dict:
    """The first pass after a (re)start — same bookkeeping as :func:`sweep`,
    with overdue legacy releases closed as ``reconciled`` so the audit
    distinguishes a downtime catch-up from a live-tick close."""
    result = sweep(
        session_exists=session_exists,
        now=now,
        overdue_reason="reconciled",
    )
    logger.info(
        "vault release reconciliation: %d release(s) closed", result["closed"],
    )
    return result
