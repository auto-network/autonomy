"""The user-carried invitation code (``AUTONOMY_INVITE``) — auto-8v5ri.

An invitation is carried by the person joining, in their own ``docker
run`` arguments, and never fetched from the relay. It names the org to
join and proves the bearer's right to claim membership:

    {v, org, root_pub, invite_ref, token}

``root_pub`` is the anchor that makes the relay a dumb transport: the
joining node pins the org's root key from the INVITATION, so a relay
that lies about which org is at the other end of the channel fails the
pin. ``token`` is the bearer secret whose SHA-256 is the invite event's
``token_hash``; it is proof of delivery and must reach only the org
node, inside the end-to-end channel.

**The bearer is redacted from every debug surface.** This value arrives
as an environment variable, so it is visible to ``docker inspect``, it
lands in process listings, and anything that logs a config object would
otherwise print it. :class:`Invitation` therefore redacts the token in
``repr``; recovering it requires asking for it by name. That is a
containment measure, not a secrecy claim — the environment itself is
readable by anyone who can inspect the container.

Decoding is total and fail-closed: every field is shape-checked before
any network call, and a truncated or edited paste fails loudly rather
than half-parsing into a request.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import uuid as uuid_mod
from dataclasses import dataclass, field

INVITE_VERSION = 1
#: Guards a truncated or mis-pasted code — a short checksum over the body.
_CHECKSUM_CHARS = 8
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class InvitationError(ValueError):
    """The invitation code is malformed, truncated, or not an invitation."""


@dataclass(frozen=True)
class Invitation:
    """A decoded invitation. ``token`` is secret; see the module docstring."""

    org: str  # the org's stable uuid
    root_pub: str  # 64-hex org signing root — the channel pin
    invite_ref: str  # 64-hex invite event id
    token: str = field(repr=False)  # bearer secret: never in repr

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Invitation(org={self.org!r}, root_pub={self.root_pub[:12]}…, "
            f"invite_ref={self.invite_ref[:12]}…, token=<redacted>)"
        )

    __str__ = __repr__

    @property
    def token_hash(self) -> str:
        """SHA-256 of the bearer — what the invite event committed to."""
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()


def _body(invitation: Invitation) -> dict:
    return {
        "v": INVITE_VERSION,
        "org": invitation.org,
        "root_pub": invitation.root_pub,
        "invite_ref": invitation.invite_ref,
        "token": invitation.token,
    }


def _checksum(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:_CHECKSUM_CHARS]


def encode_invitation(invitation: Invitation) -> str:
    """Render the code an invitee pastes into ``-e AUTONOMY_INVITE=…``."""
    raw = json.dumps(_body(invitation), sort_keys=True, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{payload}.{_checksum(raw)}"


def _require_hex64(value, what: str) -> str:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise InvitationError(f"invitation {what} must be 64 lowercase hex chars")
    return value


def decode_invitation(code: str) -> Invitation:
    """Decode and fully validate an invitation code.

    Every failure raises :class:`InvitationError` — a bad code never
    reaches the network as a half-formed request.
    """
    if not isinstance(code, str) or not code.strip():
        raise InvitationError("invitation code is empty")
    code = code.strip()
    if "." not in code:
        raise InvitationError("invitation code is missing its checksum")
    payload, _, checksum = code.rpartition(".")
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
    except (binascii.Error, ValueError) as exc:
        raise InvitationError(f"invitation code does not decode: {exc}") from exc
    if _checksum(raw) != checksum:
        raise InvitationError(
            "invitation checksum does not match — the code looks truncated or edited"
        )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvitationError(f"invitation body is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvitationError("invitation body must be an object")
    if set(data) != {"v", "org", "root_pub", "invite_ref", "token"}:
        raise InvitationError(
            "invitation body must carry exactly {v, org, root_pub, invite_ref, token}"
        )
    if data["v"] != INVITE_VERSION:
        raise InvitationError(f"unsupported invitation version: {data['v']!r}")
    try:
        uuid_mod.UUID(str(data["org"]))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvitationError("invitation org must be a UUID") from exc
    token = data["token"]
    if not isinstance(token, str) or not token:
        raise InvitationError("invitation token must be a non-empty string")
    return Invitation(
        org=str(data["org"]),
        root_pub=_require_hex64(data["root_pub"], "root_pub"),
        invite_ref=_require_hex64(data["invite_ref"], "invite_ref"),
        token=token,
    )
