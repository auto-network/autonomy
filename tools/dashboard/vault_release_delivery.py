"""Receipt-only delivery of an opened Setting into one session's PRIVATE ramfs.

The vault-open executor necessarily holds plaintext briefly while it performs
the settled server-side AES-GCM-SIV open.  This module is the only next hop:
it records a value-free release ledger entry first, then writes the value —
in its final shape, raw bytes — into the requesting session container's OWN
mount-namespace ramfs at ``/run/secrets/<credential-name>`` through one
nsenter helper over stdin (:func:`agents.secret_ramfs.deliver_secret_file`).

No shared host directory exists (the 2026-08-30 finding: a shared root gave
every sibling dashboard's sweeper the power to destroy every session's
delivery, four incidents running).  The private mount is invisible outside
the container, heals an orphaned legacy bind by mounting over it, and dies
with the container — so delivery to a long-running session needs no sweeper
protection at all.  General session output, temporary directories, and
swappable tmpfs are refused inside the helper, fail closed.
"""

from __future__ import annotations

import re
import time

from agents.secret_ramfs import (
    SESSION_SECRET_DST,
    SESSION_SECRET_UID,
    ProvisionError,
    deliver_secret_file,
)
from tools.dashboard.dao import vault_releases


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class VaultDeliveryError(RuntimeError):
    """A plaintext release could not be confined to the session's ramfs."""


def _validated_component(label: str, value: object) -> str:
    if not isinstance(value, str) or not _SAFE_COMPONENT.fullmatch(value):
        raise VaultDeliveryError(f"unsafe {label} for vault delivery")
    return value


def deliver_payload(
    row: dict,
    payload: dict,
    *,
    now: float | None = None,
) -> dict:
    """Write the released VALUE — in its final shape — into the requester's
    private in-container ramfs and return a receipt.

    The consumer receives the raw credential bytes at a stable path named
    after the credential itself (``/run/secrets/<credential-name>``, 0600) —
    a file ssh, a CLI, or the workspace's own code uses directly. No
    envelope: wrapping belongs to the store, never to the consumer, and a
    structured secret delivers its document AS the value. The file has
    SESSION LIFETIME — the kernel frees the private mount when the container
    exits; the request TTL bounds only the approval-to-delivery window.

    The durable ledger commits before the helper runs.  A crash can thus
    leave a value-free record with no file, which reconciliation closes as
    bookkeeping; it can never leave an untracked plaintext file.  A helper
    failure marks the record ``delivery_failed``.
    """
    if not isinstance(payload, dict):
        raise VaultDeliveryError("a vault Setting payload must be an object")
    value = payload.get("value")
    if not isinstance(value, str) or not value:
        raise VaultDeliveryError(
            "a vault release delivers the value in its final shape; this "
            "Setting's payload does not carry a single non-empty 'value'"
        )

    release_id = _validated_component("release id", row.get("id"))
    session = _validated_component("session", row.get("session"))
    request = row.get("request") or {}
    setting = request.get("setting") or {}
    setting_name = request.get("target")
    if not isinstance(setting_name, str) or not setting_name:
        set_id = setting.get("set_id")
        key = setting.get("key")
        if not isinstance(set_id, str) or not isinstance(key, str):
            raise VaultDeliveryError("vault release has no frozen Setting target")
        setting_name = f"{set_id}/{key}"
    # The delivered filename is the credential's own name — the suffix the
    # requester asked for, without the server-derived org prefix — so the
    # path is stable across sessions and machines and needs no lookup.
    routed_key = setting.get("key")
    if not isinstance(routed_key, str) or not routed_key:
        routed_key = setting_name.rsplit("/", 1)[-1]
    credential_name = _validated_component(
        "credential name", routed_key.rsplit(":", 1)[-1],
    )

    expires_at_s = request.get("expires_at")
    if isinstance(expires_at_s, bool) or not isinstance(expires_at_s, (int, float)):
        raise VaultDeliveryError("vault release has no valid expiry")
    stamp_s = time.time() if now is None else float(now)
    if stamp_s >= float(expires_at_s):
        raise VaultDeliveryError("vault release expired before ramfs delivery")

    container_path = f"{SESSION_SECRET_DST}/{credential_name}"
    delivered_at_ms = int(stamp_s * 1000)

    # expires_at=None: session lifetime — the container's private mount dies
    # with the container; no timer, no sweeper destruction. host_path is an
    # audit LOCATOR in the container-namespace frame: there is no host path,
    # which is the point.
    vault_releases.record_release(
        id=release_id,
        session=session,
        setting_name=setting_name,
        release_mode="delivered",
        expires_at=None,
        container_path=container_path,
        host_path=f"container-ns:{session}:{container_path}",
        delivered_at=delivered_at_ms,
    )

    encoded: bytearray | None = None
    try:
        # Encoding occurs only after the durable record. The mutable buffer
        # is wiped as soon as the helper returns; the plaintext transits the
        # helper's stdin only — never argv, env, or any host file.
        encoded = bytearray(value.encode("utf-8"))
        deliver_secret_file(session, credential_name, bytes(encoded))
    except (ProvisionError, OSError) as exc:
        vault_releases.mark_shredded(
            release_id, reason="delivery_failed", now=delivered_at_ms,
        )
        raise VaultDeliveryError(
            f"delivery into session {session!r} failed: {exc}"
        ) from exc
    finally:
        if encoded is not None:
            encoded[:] = b"\x00" * len(encoded)

    return {
        "release_id": release_id,
        "delivery": "session-ramfs",
        "path": container_path,
        "lifetime": "session",
    }
