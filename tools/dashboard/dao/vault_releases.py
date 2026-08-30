"""The durable record of secret releases (auto-pw9bs.5, auto-huzz3).

Every ``delivered``-mode secret release is recorded here BEFORE the delivery
layer materialises anything, so the only crash state is a record with no
file, never a file with no record (design ``graph://0c206bd8-1c6`` §4.2).
The record is a locator and a deadline — it never holds the plaintext, the
sealed key, or the ciphertext body: those live in the session's ramfs
subdirectory and nowhere else. A reader of this store learns which secret
went to which session and when it was destroyed, not what it was.

The rows are **machine-homed Settings** (``autonomy.vault.release-lease``
in the machine store), per the mission ruling ``d-no-bespoke-stores``:
machine-local operational state is a declared schema in ``machine.db``,
never a bespoke database file. This module replaced its own SQLite file
with that store; the first call drains any legacy ``vault_releases.db``
left by the earlier code and deletes it.

The sweeper (:mod:`tools.dashboard.vault_release_sweeper`) reads this store
to destroy releases at their deadline and to reconcile across a dashboard
restart. Destruction is recorded in place (``shredded_at`` /
``shred_reason``) rather than by deleting the row, so the record of a
release outlives the secret and a post-hoc audit can see that every
release was destroyed and why.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from tools.graph import settings_ops
from tools.graph.schemas.vault_release_lease import (
    RELEASE_LEASE_REVISION,
    RELEASE_LEASE_SET_ID,
    SHRED_REASONS,
    VaultReleaseLeaseV1,
)

__all__ = [
    "SHRED_REASONS",
    "VaultReleaseStoreError",
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

_drained = False


class VaultReleaseStoreError(RuntimeError):
    """The release record store could not be read or written."""


def _legacy_db_path() -> Path:
    """Where the retired bespoke SQLite file lived, if this machine ever ran
    the earlier code. Resolved relative to the machine store so the drain
    needs no registered Store entry for the dead file."""
    import os

    override = os.environ.get("VAULT_RELEASES_DB")
    if override:
        return Path(override)
    from tools.data_paths import resolve_store

    return resolve_store("machine").parent / "vault_releases.db"


def _drain_legacy_file() -> None:
    """One-time migration: copy any rows out of the retired SQLite file into
    the machine store, then delete the file. Value-free rows (locators,
    deadlines, shred outcomes), so a straight copy is safe; an existing
    machine-store row wins so the drain is idempotent across crashes."""
    global _drained
    if _drained:
        return
    _drained = True
    legacy = _legacy_db_path()
    if not legacy.is_file():
        return
    import sqlite3

    try:
        with sqlite3.connect(legacy, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM vault_releases").fetchall()
    except sqlite3.Error as exc:
        raise VaultReleaseStoreError(
            f"legacy vault_releases.db exists but cannot be read: {exc}"
        ) from exc
    copied = 0
    existing = {m["id"] for m in _members()}
    for row in rows:
        rec = dict(row)
        release_id = rec.pop("id")
        if release_id in existing:
            continue
        payload = {k: v for k, v in rec.items() if v is not None}
        VaultReleaseLeaseV1.validate(payload)
        _upsert(release_id, payload)
        copied += 1
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(legacy) + suffix).unlink(missing_ok=True)
        except OSError as exc:
            raise VaultReleaseStoreError(
                f"drained legacy store but could not delete {legacy}{suffix}: {exc}"
            ) from exc
    logger.info(
        "vault releases: drained %d row(s) from legacy %s into the machine "
        "store and deleted the file", copied, legacy,
    )


def _upsert(release_id: str, payload: dict) -> None:
    try:
        settings_ops.upsert_by_key(
            RELEASE_LEASE_SET_ID,
            RELEASE_LEASE_REVISION,
            release_id,
            payload,
            org=_ORG,
        )
    except Exception as exc:
        raise VaultReleaseStoreError(
            f"could not write release record: {exc}"
        ) from exc


def _members() -> list[dict]:
    try:
        members = settings_ops.read_owned_set(
            RELEASE_LEASE_SET_ID,
            org=_ORG,
            target_revision=RELEASE_LEASE_REVISION,
        ).members
    except Exception as exc:
        raise VaultReleaseStoreError(
            f"could not read release records: {exc}"
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
    _drain_legacy_file()
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
    VaultReleaseLeaseV1.validate(payload)
    with _transition_lock:
        if get(id) is not None:
            raise VaultReleaseStoreError(f"release id {id!r} already recorded")
        _upsert(id, payload)
    return get(id)


def get(release_id: str) -> dict | None:
    _drain_legacy_file()
    for rec in _members():
        if rec["id"] == release_id:
            return rec
    return None


def outstanding() -> list[dict]:
    """Every release not yet shredded — the sweeper's and reconciliation's
    work-list. Deadline rows first (oldest deadline leading); session-
    lifetime rows (no deadline) after them."""
    _drain_legacy_file()
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
        VaultReleaseLeaseV1.validate(payload)
        _upsert(release_id, payload)
    return True
