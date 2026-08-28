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

import json
import os
import re
import stat
import time
from pathlib import Path

from agents.secret_ramfs import (
    DELIVERY_MOUNT,
    SESSION_SECRET_DST,
    SESSION_SECRET_UID,
    provision_session_dir,
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
    """Write *payload* to the exact requester's ramfs and return a receipt.

    The durable ledger commits before the file is created.  A crash can thus
    leave a value-free record with no file, which reconciliation can close;
    it can never leave an untracked plaintext file.  Any post-record failure
    removes a partial file and marks the receipt ``delivery_failed``.
    """
    if not isinstance(payload, dict):
        raise VaultDeliveryError("a vault Setting payload must be an object")

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

    expires_at_s = request.get("expires_at")
    if isinstance(expires_at_s, bool) or not isinstance(expires_at_s, (int, float)):
        raise VaultDeliveryError("vault release has no valid expiry")
    stamp_s = time.time() if now is None else float(now)
    if stamp_s >= float(expires_at_s):
        raise VaultDeliveryError("vault release expired before ramfs delivery")

    root = Path(delivery_root) if delivery_root is not None else _delivery_root()
    session_dir = root / session
    if not session_dir.is_dir() or session_dir.is_symlink():
        # The launcher provisioned this directory at session start; a sweeper
        # or host hiccup can remove it while the session is still live. The
        # provisioning helper is idempotent — re-create it rather than fail
        # the release. (A container whose bind was orphaned by the removal
        # still fails visibly: the receipt path never materialises inside it.)
        if delivery_root is None:
            try:
                provision_session_dir(session, SESSION_SECRET_UID)
            except Exception as exc:
                raise VaultDeliveryError(
                    "the requesting session has no secret ramfs"
                ) from exc
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

    filename = f"vault-open-{release_id}.json"
    host_path = session_dir / filename
    container_path = str(Path(SESSION_SECRET_DST) / filename)
    expires_at_ms = int(float(expires_at_s) * 1000)
    delivered_at_ms = int(stamp_s * 1000)

    vault_releases.record_release(
        id=release_id,
        session=session,
        setting_name=setting_name,
        release_mode="delivered",
        expires_at=expires_at_ms,
        container_path=container_path,
        host_path=str(host_path),
        delivered_at=delivered_at_ms,
    )

    fd: int | None = None
    encoded: bytearray | None = None
    try:
        # Encoding occurs only after the ramfs guard and durable receipt.  The
        # mutable buffer is wiped immediately after the kernel accepts it.
        encoded = bytearray(json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8"))
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
        "expires_at": expires_at_s,
    }
