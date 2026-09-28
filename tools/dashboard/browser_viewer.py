"""Operator viewer and control handover for browser leases (auto-8q7oe.7).

The dashboard relays VNC between the operator's browser (noVNC over a
WebSocket) and a lease container's x11vnc on the lease network. The relay
answers the lease's VNC password challenge itself and offers the browser a
no-authentication session, so the password never leaves the dashboard.

Every client message is parsed. Key, pointer, clipboard and desktop-size
messages from a connection that does not hold control are dropped; any
message type the relay does not know closes the connection.

Control: one viewer holds it. Taking control locks the lease (holder human)
by compare-and-set and stops an agent command in flight. When the holder
disconnects, control stays with the operator for :data:`GRACE_S` seconds and
then returns to the agent. Design: graph://c330323d-986.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

from tools.dashboard import browser_containers as containers
from tools.dashboard import browser_reconciler as reconciler
from tools.dashboard.dao import browser_leases as store

logger = logging.getLogger(__name__)

VNC_PORT = 5900
GRACE_S = 60.0
SESSION_RECHECK_S = 10.0
ACTIVITY_WRITE_S = 5.0
MAX_CUT_TEXT = 1 << 20
RFB_VERSION = b"RFB 003.008\n"


class ProtocolError(RuntimeError):
    pass


# ── VNC authentication ─────────────────────────────────────────────────


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """RFB 'VNC authentication': DES-encrypt the 16-byte challenge with the
    password (first 8 bytes, zero-padded, each byte's bits reversed)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:  # pragma: no cover - older cryptography
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in password.encode()[:8].ljust(8, b"\0"))
    encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return encryptor.update(challenge) + encryptor.finalize()


# ── client message filter ──────────────────────────────────────────────

_INPUT_TYPES = frozenset({4, 5, 6, 251})  # key, pointer, cut text, SetDesktopSize


class ClientFilter:
    """Split the browser's byte stream into RFB client messages and decide
    which reach the lease. Partial messages are buffered until complete."""

    def __init__(self):
        self.buffer = b""

    @staticmethod
    def _length(buf: bytes) -> Optional[int]:
        """Full length of the message at the start of *buf*, or None if more
        bytes are needed to know it."""
        kind = buf[0]
        fixed = {0: 20, 3: 10, 4: 8, 5: 6, 150: 10}
        if kind in fixed:
            return fixed[kind]
        if kind == 2:  # SetEncodings
            return None if len(buf) < 4 else 4 + 4 * struct.unpack(">H", buf[2:4])[0]
        if kind == 6:  # ClientCutText
            if len(buf) < 8:
                return None
            # Signed: a negative length marks the extended-clipboard format,
            # whose payload is abs(length) bytes (noVNC sends one on connect).
            size = abs(struct.unpack(">i", buf[4:8])[0])
            if size > MAX_CUT_TEXT:
                raise ProtocolError("clipboard text too large")
            return 8 + size
        if kind == 248:  # ClientFence
            return None if len(buf) < 9 else 9 + buf[8]
        if kind == 251:  # SetDesktopSize
            return None if len(buf) < 8 else 8 + 16 * buf[6]
        raise ProtocolError(f"unknown RFB client message type {kind}")

    def feed(self, data: bytes, allow_input: bool) -> tuple[bytes, bool]:
        """Return ``(bytes to forward, whether any input message arrived)``."""
        self.buffer += data
        forward, saw_input = bytearray(), False
        while self.buffer:
            length = self._length(self.buffer)
            if length is None or len(self.buffer) < length:
                break
            message, self.buffer = self.buffer[:length], self.buffer[length:]
            if message[0] in _INPUT_TYPES:
                saw_input = True
                if not allow_input:
                    continue
            forward += message
        return bytes(forward), saw_input


# ── control state (this worker) ────────────────────────────────────────


@dataclass
class _Control:
    holder: Optional[str] = None           # viewer id holding control
    grace: Optional[asyncio.TimerHandle] = None
    viewers: set = field(default_factory=set)


_controls: dict[str, _Control] = {}


def _control(lease_hash: str) -> _Control:
    return _controls.setdefault(lease_hash, _Control())


def find_lease(lease_ref: str) -> Optional[store.Lease]:
    """An active lease by its operator reference (the first 16 hex characters
    of its hash); the caller's lease id never appears in operator URLs."""
    if len(lease_ref) != 16 or any(c not in "0123456789abcdef" for c in lease_ref):
        return None
    matches = [l for l in store.list_leases() if l.lease_hash.startswith(lease_ref)]
    return matches[0] if len(matches) == 1 else None


class ControlRefused(RuntimeError):
    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status, self.error = status, error


def take_control(lease: store.Lease, viewer: str) -> dict:
    """Give *viewer* control: lock the lease for a human, stopping an agent
    command in flight. Taking it from another viewer moves it."""
    epoch = reconciler.epoch()
    if epoch is None:
        raise ControlRefused(503, "broker-starting")
    state = _control(lease.lease_hash)
    if lease.state == "locked" and lease.lock_holder == "human":
        state.holder = viewer
        _cancel_grace(state)
        return {"state": "locked", "holder": "human"}
    if lease.state not in ("ready", "busy"):
        raise ControlRefused(409, f"lease is {lease.state}")
    # Fail closed: the agent is locked FIRST, so the dashboard never shows the
    # operator in control while the agent still accepts commands. A locked
    # agent refuses every command and stops the running one, even one that
    # won the busy compare-and-set a moment before (auto-8q7oe.6).
    if not containers.agent_lock(lease, True):
        raise ControlRefused(502, "the lease agent did not confirm the lock; control not taken")
    # Either state: locking the agent stops a running command, whose route then
    # moves the row busy -> ready while we get here.
    if not store.transition(lease.lease_hash, epoch=epoch, to="locked", expect=("ready", "busy"),
                            audit_op="control", result="take", lock_holder="human",
                            last_activity=time.time()):
        containers.agent_lock(lease, False)
        raise ControlRefused(409, "lease changed; retry")
    state.holder = viewer
    _cancel_grace(state)
    return {"state": "locked", "holder": "human"}


def return_control(lease: store.Lease, viewer: Optional[str]) -> dict:
    """Hand control back to the agent. *viewer* None is the grace expiry."""
    epoch = reconciler.epoch()
    if epoch is None:
        raise ControlRefused(503, "broker-starting")
    state = _control(lease.lease_hash)
    if viewer is not None and state.holder not in (None, viewer):
        raise ControlRefused(409, "another viewer holds control")
    if not (lease.state == "locked" and lease.lock_holder == "human"):
        state.holder = None
        return {"state": lease.state}
    # The password cleanup runs before the agent regains control (a sign-in
    # may have handed the lease over mid-login). Unconfirmed: control stays.
    if not _agent_cleanup(lease):
        raise ControlRefused(502, "password cleanup not confirmed; control stays with the operator")
    containers.agent_lock(lease, False)
    if not store.transition(lease.lease_hash, epoch=epoch, to="ready", expect=("locked",),
                            audit_op="control", result="return", lock_holder=None,
                            last_activity=time.time()):
        raise ControlRefused(409, "lease changed; retry")
    state.holder = None
    _cancel_grace(state)
    return {"state": "ready"}


def _agent_cleanup(lease: store.Lease) -> bool:
    if not lease.address:
        return False
    try:
        status, reply = containers.agent_request(lease.address, lease.secret, "POST",
                                                 "/login/cleanup", {}, timeout=65)
    except Exception:
        return False
    return status == 200 and reply.get("cleaned") is True


def _cancel_grace(state: _Control) -> None:
    if state.grace is not None:
        state.grace.cancel()
        state.grace = None


def viewer_left(lease_hash: str, viewer: str, loop: asyncio.AbstractEventLoop) -> None:
    """The holder's connection closed: return control after the grace period
    unless it (or another viewer) takes control again first."""
    state = _control(lease_hash)
    state.viewers.discard(viewer)
    if state.holder != viewer:
        return
    _cancel_grace(state)

    def expire():
        state.grace = None
        if state.holder != viewer:
            return
        lease = store.get(lease_hash)
        if lease is not None:
            loop.run_in_executor(None, _return_after_grace, lease_hash)

    state.grace = loop.call_later(GRACE_S, expire)


def _return_after_grace(lease_hash: str) -> None:
    lease = store.get(lease_hash)
    if lease is None:
        return
    try:
        return_control(lease, None)
    except ControlRefused as exc:
        logger.info("browser viewer: grace return for %s skipped: %s", lease.container_name, exc)


def holds_control(lease_hash: str, viewer: str) -> bool:
    return _control(lease_hash).holder == viewer


# ── who may open a viewer ──────────────────────────────────────────────


def same_origin(origin: Optional[str], host: Optional[str]) -> bool:
    """The dashboard's own origin only: a cross-site page cannot open the
    viewer or change control even with the operator's cookie attached."""
    return bool(origin and host) and origin in (f"https://{host}", f"http://{host}")


def organization_bearer(authorization: Optional[str]) -> bool:
    """Whether the request carries an organization session's token; those are
    refused (403) on every operator route, including the WebSocket upgrade."""
    if not authorization or not authorization.startswith("Bearer ") or not authorization[7:]:
        return False
    import hashlib

    from tools.dashboard.dao import auth_db

    resolved = auth_db.resolve_token(hashlib.sha256(authorization[7:].encode()).hexdigest())
    return resolved is not None and resolved[1] is not None
