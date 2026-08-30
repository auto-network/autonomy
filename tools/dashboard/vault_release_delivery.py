"""Receipt-only delivery of an opened Setting into one session's ramfs.

The vault-open executor necessarily holds plaintext briefly while it performs
the settled server-side AES-GCM-SIV open.  This module is the only next hop:
it refuses every destination except the launcher's per-session **ramfs**
directory, records a value-free release ledger entry first, materialises one
mode-0600 file, and returns only a receipt naming the container-visible path.

General session output, temporary directories, and tmpfs are deliberately not
fallbacks.  A missing or incorrectly mounted ramfs turns delivery into a
failure before plaintext bytes are encoded for writing.
"""

from __future__ import annotations

import os
import re
import stat
import time
from pathlib import Path

from agents.secret_ramfs import (
    DELIVERY_MOUNT,
    SESSION_SECRET_DST,
    SESSION_SECRET_UID,
)
from tools.dashboard.dao import vault_releases
from tools.network.storagekit.memory_cache import assert_memory_backed


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class VaultDeliveryError(RuntimeError):
    """A plaintext release could not be confined to session ramfs."""


def _validated_component(label: str, value: object) -> str:
    if not isinstance(value, str) or not _SAFE_COMPONENT.fullmatch(value):
        raise VaultDeliveryError(f"unsafe {label} for vault delivery")
    return value


def _delivery_root() -> Path:
    return Path(DELIVERY_MOUNT)


def _write_all(fd: int, encoded: bytearray) -> None:
    """Write a mutable buffer fully (separate seam for failure testing)."""
    view = memoryview(encoded)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("short write while delivering vault payload")
        written += count


def deliver_payload(
    row: dict,
    payload: dict,
    *,
    delivery_root: Path | None = None,
    now: float | None = None,
) -> dict:
    """Write the released VALUE — in its final shape — to the requester's
    ramfs and return a receipt.

    The consumer receives the raw credential bytes at a stable path named
    after the credential itself (``/run/secrets/<credential-name>``), mode
    0600 — a file ssh, a CLI, or the workspace's own code can use directly.
    No envelope: wrapping belongs to the store, never to the consumer, and
    a structured secret delivers its document AS the value. The file has
    SESSION LIFETIME — it is destroyed at session end (or by the orphan
    sweep), not on a short timer; the request TTL bounds only the
    approval-to-delivery window.

    The durable ledger commits before the file is created.  A crash can thus
    leave a value-free record with no file, which reconciliation can close;
    it can never leave an untracked plaintext file.  Any post-record failure
    removes a partial file and marks the receipt ``delivery_failed``.
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

    root = Path(delivery_root) if delivery_root is not None else _delivery_root()
    session_dir = root / session
    if not session_dir.is_dir() or session_dir.is_symlink():
        raise VaultDeliveryError("the requesting session has no secret ramfs")
    directory_stat = session_dir.stat()
    if stat.S_IMODE(directory_stat.st_mode) != 0o700:
        raise VaultDeliveryError("the requesting session secret ramfs is not mode 0700")
    if directory_stat.st_uid != SESSION_SECRET_UID:
        raise VaultDeliveryError("the requesting session secret ramfs has the wrong owner")
    # Check the actual destination on every write.  This is the production
    # gate that refuses both ordinary disk and swappable tmpfs.
    assert_memory_backed(session_dir)

    host_path = session_dir / credential_name
    container_path = str(Path(SESSION_SECRET_DST) / credential_name)
    delivered_at_ms = int(stamp_s * 1000)

    # expires_at=None: session lifetime. The launcher's session-end hook and
    # the sweeper's orphan pass destroy the file; no short timer applies to
    # the artifact (the TTL above bounded only approval-to-delivery).
    vault_releases.record_release(
        id=release_id,
        session=session,
        setting_name=setting_name,
        release_mode="delivered",
        expires_at=None,
        container_path=container_path,
        host_path=str(host_path),
        delivered_at=delivered_at_ms,
    )

    fd: int | None = None
    encoded: bytearray | None = None
    try:
        # Encoding occurs only after the ramfs guard and durable receipt.  The
        # mutable buffer is wiped immediately after the kernel accepts it.
        # RAW value bytes — never an envelope the consumer would have to
        # unwrap.
        encoded = bytearray(value.encode("utf-8"))
        # A re-release of the same credential in the same session replaces
        # the file (fresh O_EXCL create after unlink, so a symlink can never
        # be followed). The superseded lease still points here; destruction
        # tolerates an already-gone file, so both leases settle at session
        # end.
        try:
            os.unlink(host_path)
        except FileNotFoundError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(host_path, flags, 0o600)
        _write_all(fd, encoded)
        os.close(fd)
        fd = None
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            host_path.unlink()
        except FileNotFoundError:
            pass
        vault_releases.mark_shredded(
            release_id,
            reason="delivery_failed",
                now=delivered_at_ms,
        )
        raise
    finally:
        if encoded is not None:
            encoded[:] = b"\x00" * len(encoded)

    return {
        "release_id": release_id,
        "delivery": "session-ramfs",
        "path": container_path,
        "lifetime": "session",
    }
