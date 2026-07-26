"""The user-carried invitation code (``AUTONOMY_INVITE``) — auto-8v5ri.

An invitation is carried by the person joining, in their own ``docker
run`` arguments, and never fetched from the relay. It names the org to
join, carries the relay grant that reaches that org, and proves the
bearer's right to claim membership:

    {v, org, root_pub, invite_ref, channel_token, claim_token}

``root_pub`` is the anchor that makes the relay a dumb transport: the
joining node pins the org's root key from the INVITATION, so a relay
that lies about which org is at the other end of the channel fails the
pin. ``channel_token`` is the registry-minted transport credential from the
join URL's path; the relay is allowed to see it. ``claim_token`` is the
separate bearer secret from the URL fragment whose SHA-256 is the invite
event's ``token_hash``; it is proof of delivery and must reach only the org
node, inside the end-to-end channel. The two tokens are deliberately
different security domains and may never be collapsed.

**Both credentials are redacted from every debug surface.** They arrive as
one environment value, so they are visible to ``docker inspect``, land in
process listings, and anything that logs a config object would otherwise
print them. :class:`Invitation` therefore redacts both in ``repr``;
recovering either requires asking for it by name. That is a containment
measure, not a secrecy claim — the environment itself is readable by anyone
who can inspect the container.

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
import urllib.parse
import uuid as uuid_mod
from dataclasses import dataclass, field

INVITE_VERSION = 2
#: Guards a truncated or mis-pasted code — a short checksum over the body.
_CHECKSUM_CHARS = 8
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CHANNEL_TOKEN = re.compile(r"^[0-9a-f]{32}$")


class InvitationError(ValueError):
    """The invitation code is malformed, truncated, or not an invitation."""


@dataclass(frozen=True)
class Invitation:
    """A decoded invitation. Both token fields are secret."""

    org: str  # the org's stable uuid
    root_pub: str  # 64-hex org signing root — the channel pin
    invite_ref: str  # 64-hex invite event id
    channel_token: str = field(repr=False)  # relay-visible transport credential
    claim_token: str = field(repr=False)  # E2E-only ledger bearer

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Invitation(org={self.org!r}, root_pub={self.root_pub[:12]}…, "
            f"invite_ref={self.invite_ref[:12]}…, "
            "channel_token=<redacted>, claim_token=<redacted>)"
        )

    __str__ = __repr__

    @property
    def token_hash(self) -> str:
        """SHA-256 of the bearer — what the invite event committed to."""
        return hashlib.sha256(self.claim_token.encode("utf-8")).hexdigest()


def _body(invitation: Invitation) -> dict:
    return {
        "v": INVITE_VERSION,
        "org": invitation.org,
        "root_pub": invitation.root_pub,
        "invite_ref": invitation.invite_ref,
        "channel_token": invitation.channel_token,
        "claim_token": invitation.claim_token,
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


def _require_tokens(channel_token, claim_token) -> tuple[str, str]:
    if (
        not isinstance(channel_token, str)
        or not _CHANNEL_TOKEN.fullmatch(channel_token)
    ):
        raise InvitationError(
            "invitation channel_token must be 32 lowercase hex chars"
        )
    if not isinstance(claim_token, str) or not claim_token:
        raise InvitationError("invitation claim_token must be a non-empty string")
    if len(claim_token) > 128:
        raise InvitationError("invitation claim_token is too long")
    if channel_token == claim_token:
        raise InvitationError(
            "invitation transport grant and ledger bearer must be distinct"
        )
    return channel_token, claim_token


def invitation_from_join_url(
    *,
    org: str,
    root_pub: str,
    invite_ref: str,
    join_url: str,
) -> Invitation:
    """Assemble an invitation v2 from a minted ``org:join`` URL.

    The registry grant comes from ``/l/<grant>`` in the request path. The
    ledger bearer comes from the client-only ``#t=<bearer>`` fragment. Parsing
    those positions here keeps the domain split identical in the CLI minter
    and the B6 harness driver.
    """
    if not isinstance(join_url, str):
        raise InvitationError("invitation join URL must be a string")
    parsed = urllib.parse.urlsplit(join_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise InvitationError("invitation join URL is malformed")
    parts = parsed.path.rstrip("/").split("/")
    if len(parts) < 3 or parts[-2] != "l":
        raise InvitationError("invitation join URL has no registry grant path")
    try:
        fragment = urllib.parse.parse_qs(
            parsed.fragment,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise InvitationError("invitation join URL fragment is malformed") from exc
    if set(fragment) != {"t"} or len(fragment["t"]) != 1:
        raise InvitationError(
            "invitation join URL must carry exactly one fragment bearer"
        )
    channel_token, claim_token = _require_tokens(parts[-1], fragment["t"][0])
    invitation = Invitation(
        org=org,
        root_pub=root_pub,
        invite_ref=invite_ref,
        channel_token=channel_token,
        claim_token=claim_token,
    )
    # Reuse the full decoder's public-field validation.
    return decode_invitation(encode_invitation(invitation))


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
    if data.get("v") == 1:
        raise InvitationError(
            "invitation version 1 cannot open the production relay channel; "
            "regenerate the invitation"
        )
    if set(data) != {
        "v", "org", "root_pub", "invite_ref", "channel_token", "claim_token",
    }:
        raise InvitationError(
            "invitation body must carry exactly "
            "{v, org, root_pub, invite_ref, channel_token, claim_token}"
        )
    if data["v"] != INVITE_VERSION:
        raise InvitationError(f"unsupported invitation version: {data['v']!r}")
    try:
        uuid_mod.UUID(str(data["org"]))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvitationError("invitation org must be a UUID") from exc
    channel_token, claim_token = _require_tokens(
        data["channel_token"], data["claim_token"]
    )
    return Invitation(
        org=str(data["org"]),
        root_pub=_require_hex64(data["root_pub"], "root_pub"),
        invite_ref=_require_hex64(data["invite_ref"], "invite_ref"),
        channel_token=channel_token,
        claim_token=claim_token,
    )
