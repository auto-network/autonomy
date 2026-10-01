"""The vault audit table: one row per delivered vault secret (auto-pw9bs.5,
auto-huzz3; named the audit table by auto-njfhs).

Every ``delivered``-mode release writes its row HERE, BEFORE the delivery
layer materialises anything, so the only crash state is a row with no file,
never a file with no row (design ``graph://0c206bd8-1c6`` §4.2). A row says
which secret went to which session, where the copy was placed, its deadline,
and -- once destroyed -- when and why (``shredded_at`` / ``shred_reason``).
It never holds the plaintext, the sealed key, or the ciphertext: those live
in the session's private ramfs and nowhere else.

The rows are **machine-homed Settings** (``autonomy.vault.audit`` in the
machine store), per the mission ruling ``d-no-bespoke-stores``.

The sweeper (:mod:`tools.dashboard.vault_release_sweeper`) reads the
outstanding rows to destroy copies at their deadline and to reconcile across
a dashboard restart. Destruction is recorded in place rather than by
deleting the row, so the audit of a delivery outlives the secret.
"""

from __future__ import annotations

import logging
import threading
import time

from tools.graph import settings_ops
from tools.graph.schemas.vault_audit import (
    VAULT_AUDIT_REVISION,
    VAULT_AUDIT_SET_ID,
    SHRED_REASONS,
    VaultAuditV1,
)

__all__ = [
    "SHRED_REASONS",
    "VaultAuditStoreError",
    "record_release",
    "get",
    "outstanding",
    "outstanding_sessions",
    "mark_shredded",
]

logger = logging.getLogger(__name__)

_ORG = "machine"

#: Serialises read-modify-write transitions (``mark_shredded``) within this
#: process. The executor, the launcher's session-end hook, and the sweeper
#: all run in the one dashboard process; the lock preserves the
#: first-destruction-wins property the old store enforced with a
#: conditional UPDATE.
_transition_lock = threading.Lock()



class VaultAuditStoreError(RuntimeError):
    """The vault audit table could not be read or written."""


def _upsert(release_id: str, payload: dict) -> None:
    try:
        settings_ops.upsert_by_key(
            VAULT_AUDIT_SET_ID,
            VAULT_AUDIT_REVISION,
            release_id,
            payload,
            org=_ORG,
        )
    except Exception as exc:
        raise VaultAuditStoreError(
            f"could not write vault audit row: {exc}"
        ) from exc


def _members() -> list[dict]:
    try:
        members = settings_ops.read_owned_set(
            VAULT_AUDIT_SET_ID,
            org=_ORG,
            target_revision=VAULT_AUDIT_REVISION,
        ).members
    except Exception as exc:
        raise VaultAuditStoreError(
            f"could not read vault audit rows: {exc}"
        ) from exc
    rows = []
    for member in members:
        if not isinstance(member.payload, dict):
            continue
        rec = dict(member.payload)
        rec.setdefault("shredded_at", None)
        rec.setdefault("shred_reason", None)
        rec.setdefault("expires_at", None)  # absent = session lifetime
        rec["id"] = member.key
        rows.append(rec)
    return rows


def record_release(
    *,
    id: str,
    session: str,
    setting_name: str,
    release_mode: str,
    expires_at: int | None,
    container_path: str,
    host_path: str,
    delivered_at: int | None = None,
    now: int | None = None,
) -> dict:
    """Commit a release record. THIS RUNS BEFORE THE DELIVERY LAYER RETURNS.

    The ordering is the invariant the whole store exists for: the caller
    must commit the record here and only then materialise the ramfs file, so
    a crash between the two leaves a record with no file — a state the
    sweeper cleans — and never a file with no record, which nothing would
    ever find. A caller that materialises first and records second has
    silently defeated the store; the delivery bead's contract is
    record-then-deliver, asserted there.

    ``host_path`` is where the SWEEPER unlinks — the file's location on the
    host, inside the per-session ramfs subdirectory
    (``agents.secret_ramfs.DELIVERY_MOUNT/<session>/...``). ``container_path``
    is the same file as the session sees it, carried for the audit only.
    Neither is the secret; both are paths.
    """
    if release_mode != "delivered":
        # Only 'delivered' mode materialises a file that needs sweeping;
        # 'mediated' and 'client_operation' leave no host artifact, so a
        # record here would describe a file that never exists. Refuse
        # rather than record a shred target the sweeper can never satisfy.
        raise ValueError(
            f"only 'delivered' releases are recorded here, not {release_mode!r}"
        )
    for name, value in (
        ("id", id), ("session", session), ("setting_name", setting_name),
        ("container_path", container_path), ("host_path", host_path),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
    if expires_at is not None and (
        not isinstance(expires_at, int) or isinstance(expires_at, bool)
    ):
        raise ValueError("expires_at must be an int (unix ms) or None "
                         "(session lifetime)")
    stamp = int(time.time() * 1000) if now is None else int(now)
    delivered = stamp if delivered_at is None else int(delivered_at)
    payload = {
        "session": session,
        "setting_name": setting_name,
        "release_mode": release_mode,
        "delivered_at": delivered,
        "container_path": container_path,
        "host_path": host_path,
    }
    if expires_at is not None:
        payload["expires_at"] = int(expires_at)
    VaultAuditV1.validate(payload)
    with _transition_lock:
        if get(id) is not None:
            raise VaultAuditStoreError(f"release id {id!r} already recorded")
        _upsert(id, payload)
    return get(id)


def get(release_id: str) -> dict | None:
    for rec in _members():
        if rec["id"] == release_id:
            return rec
    return None


def outstanding() -> list[dict]:
    """Every release not yet shredded — the sweeper's and reconciliation's
    work-list. Deadline rows first (oldest deadline leading); session-
    lifetime rows (no deadline) after them."""
    rows = [rec for rec in _members() if rec["shredded_at"] is None]
    rows.sort(key=lambda rec: (
        rec["expires_at"] is None, rec["expires_at"] or 0, rec["id"],
    ))
    return rows


def outstanding_sessions() -> set[str]:
    """The distinct sessions with any outstanding release — used to decide
    which per-session ramfs subdirectories are still live."""
    return {rec["session"] for rec in outstanding()}


def mark_shredded(
    release_id: str,
    *,
    reason: str,
    now: int | None = None,
) -> bool:
    """Record that a release's file was destroyed. Idempotent: a second
    call on an already-shredded row keeps the FIRST reason and timestamp,
    because the first destruction is the true one and a re-sweep must not
    rewrite history. Returns True iff this call performed the transition."""
    if reason not in SHRED_REASONS:
        raise ValueError(
            f"shred reason {reason!r} not in {sorted(SHRED_REASONS)}"
        )
    stamp = int(time.time() * 1000) if now is None else int(now)
    with _transition_lock:
        rec = get(release_id)
        if rec is None or rec["shredded_at"] is not None:
            return False
        payload = {
            k: v for k, v in rec.items()
            if k not in ("id", "shredded_at", "shred_reason")
            # A session-lifetime release surfaces expires_at=None from
            # _members(); the stored row simply omits the field.
            and not (k == "expires_at" and rec[k] is None)
        }
        payload["shredded_at"] = stamp
        payload["shred_reason"] = reason
        VaultAuditV1.validate(payload)
        _upsert(release_id, payload)
    return True
