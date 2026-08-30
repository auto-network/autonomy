"""Destroy delivered credentials at their TTL, and close the ledger.

A vault release delivers a credential into the requesting container's OWN
private ramfs (``agents.secret_ramfs.deliver_secret_file``) with a lifetime
the requester chose (``ttl_seconds``). This module enforces that lifetime:
when a lease is past its deadline it removes THAT ONE file from THAT ONE
container, addressed by the exact ``(session, container_path)`` the durable
lease recorded — via ``destroy_secret_file``'s nsenter unlink.

Why this cannot repeat the four-incident shared-root class (Aug 23–30): it
never enumerates a directory and never infers what to delete from liveness.
The old design kept every session's secret in ONE shared host ramfs that any
dashboard could scan and reclaim on its own machine-local tmux guess, so a
sibling dashboard destroyed every live session's secrets. Here, destruction
is per-file, driven by this node's own ledger, and reaches only the exact
path this node delivered. A file for a container that has already exited is
gone with the kernel-freed mount — absence is success.

``session_exists`` is injected for the orphan-close bookkeeping only (a lease
whose session vanished before its deadline); it never drives destruction.
``None`` means liveness was unanswerable this tick — those bookkeeping
closes simply wait; deadline destruction is unaffected because it needs no
liveness at all.
"""

from __future__ import annotations

import logging
import posixpath
import time
from typing import Callable

from tools.dashboard.dao import vault_releases

logger = logging.getLogger(__name__)

#: How often the live sweep runs. A credential is destroyed by its TTL
#: deadline plus at most one interval.
SWEEP_INTERVAL_S = 30

#: Prefix of the RETIRED shared delivery root. A legacy lease's ``host_path``
#: under it is a real on-disk file and gets a best-effort unlink; the
#: ``container-ns:`` locators the current design records are not host paths.
_LEGACY_HOST_PREFIX = "/run/autonomy-secrets/"


def _destroy_delivered(rec: dict) -> None:
    """Destroy the credential a lease delivered, by its exact address.

    Current lease: nsenter-unlink the recorded file inside its container
    (no-op if the container is gone). Legacy shared-root lease: unlink the
    real host path it recorded. Best-effort — a destruction failure leaves
    the lease outstanding for the next tick rather than lying that it closed.
    """
    from agents import secret_ramfs
    from agents.secret_ramfs import ProvisionError

    host_path = rec.get("host_path") or ""
    if host_path.startswith(_LEGACY_HOST_PREFIX):
        from pathlib import Path
        try:
            Path(host_path).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("vault sweep: could not unlink legacy %s", host_path)
        return
    filename = posixpath.basename(rec.get("container_path") or "")
    if not filename:
        return
    secret_ramfs.destroy_secret_file(rec["session"], filename)


def sweep(
    *,
    session_exists: "Callable[[str], bool] | None",
    now: int | None = None,
    overdue_reason: str = "expired",
) -> dict:
    """One pass over the outstanding leases.

    * a lease past its TTL deadline: destroy the exact delivered file, then
      close it *overdue_reason*;
    * a lease whose SESSION is gone (its private mount already freed by the
      kernel): close it ``orphaned`` — pure bookkeeping, no file to touch.

    ``session_exists=None`` leaves the orphan-close bookkeeping for a later
    tick (an ambiguous liveness answer never drives a transition); deadline
    destruction still runs, since it needs no liveness.

    Returns ``{destroyed, closed}``.
    """
    stamp = int(time.time() * 1000) if now is None else int(now)

    destroyed = 0
    closed = 0
    for rec in vault_releases.outstanding():
        session = rec["session"]
        deadline = rec.get("expires_at")
        past_deadline = deadline is not None and stamp >= deadline
        gone = session_exists is not None and not session_exists(session)
        if past_deadline:
            reason = overdue_reason
        elif gone:
            reason = "orphaned"
        else:
            continue
        try:
            if past_deadline:
                _destroy_delivered(rec)
                destroyed += 1
        except Exception:
            logger.exception(
                "vault sweep: could not destroy delivered credential for "
                "release %s (session=%s) — left outstanding for retry",
                rec["id"], session,
            )
            continue
        if vault_releases.mark_shredded(rec["id"], reason=reason, now=stamp):
            closed += 1
            logger.info(
                "vault sweep: closed release %s (session=%s reason=%s)",
                rec["id"], session, reason,
            )
    return {"destroyed": destroyed, "closed": closed}


def on_session_end(session: str, *, now: int | None = None) -> dict:
    """The launcher's timely session-end hook: close the session's
    outstanding leases as ``session_end``. The delivered files died with the
    container's private mount, so there is nothing to destroy here — only the
    ledger to close (plus a best-effort unlink of any legacy shared-root path
    a lease still names). Idempotent and race-safe with the sweep."""
    stamp = int(time.time() * 1000) if now is None else int(now)
    closed = 0
    for rec in vault_releases.outstanding():
        if rec["session"] != session:
            continue
        host_path = rec.get("host_path") or ""
        if host_path.startswith(_LEGACY_HOST_PREFIX):
            from pathlib import Path
            try:
                Path(host_path).unlink()
            except (FileNotFoundError, OSError):
                pass
        if vault_releases.mark_shredded(rec["id"], reason="session_end",
                                        now=stamp):
            closed += 1
    return {"closed": closed}


def reconcile_on_startup(
    *,
    session_exists: "Callable[[str], bool] | None",
    now: int | None = None,
) -> dict:
    """The first pass after a (re)start — same as :func:`sweep`, with overdue
    leases closed as ``reconciled`` so the audit distinguishes a downtime
    catch-up from a live-tick close. A lease whose container is gone had its
    file freed with the mount; one still delivered but past its TTL is
    destroyed now."""
    result = sweep(
        session_exists=session_exists,
        now=now,
        overdue_reason="reconciled",
    )
    logger.info(
        "vault release reconciliation: %d destroyed, %d release(s) closed",
        result["destroyed"], result["closed"],
    )
    return result
