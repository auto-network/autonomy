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
_CHANNEL_FRAGMENT = re.compile(r"^[A-Za-z0-9_-]{43}$")

#: The two independent fragment keys of a viewer invitation URL
#: (graph://4f9e881c-a9 §3). ``k`` carries the per-link channel-verification
#: PUBLIC key (authenticates the serving endpoint); ``t`` carries the
#: invitation bearer (buys only the right to ASK to join). The two are
#: separate authorities and are never conflated: a link missing either value
#: is not an apparently usable invitation, and ``root_pub`` is NEVER a
#: substitute for ``k``.
INVITE_FRAGMENT_CHANNEL_KEY = "k"
INVITE_FRAGMENT_BEARER = "t"


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


def encode_channel_pub(channel_pub_hex: str) -> str:
    """64-hex Ed25519 public key → the fragment ``k`` value.

    Unpadded base64url of the 32 raw bytes (43 chars), the SAME encoding the
    per-link channel key already uses on content-share fragments
    (``link_channel_key.fragment_url`` / ``pub_from_fragment``). Keeping one
    encoding means the viewer decodes ``k`` exactly as it decodes a content
    link's channel key.
    """
    if not isinstance(channel_pub_hex, str) or not _HEX64.match(channel_pub_hex):
        raise InvitationError("channel public key must be 64 lowercase hex chars")
    raw = bytes.fromhex(channel_pub_hex)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_channel_pub(fragment_value: str) -> str:
    """Inverse of :func:`encode_channel_pub`; raises on anything that is not
    exactly the canonical 43-character unpadded base64url spelling of a
    32-byte value."""
    if (
        not isinstance(fragment_value, str)
        or not _CHANNEL_FRAGMENT.fullmatch(fragment_value)
    ):
        raise InvitationError(
            "fragment channel key must be 43 unpadded base64url characters"
        )
    pad = "=" * (-len(fragment_value) % 4)
    try:
        raw = base64.urlsafe_b64decode(fragment_value + pad)
    except (binascii.Error, ValueError) as exc:
        raise InvitationError("fragment channel key does not decode") from exc
    if len(raw) != 32:
        raise InvitationError("fragment channel key is not a 32-byte value")
    public_hex = raw.hex()
    if encode_channel_pub(public_hex) != fragment_value:
        raise InvitationError("fragment channel key is not canonically encoded")
    return public_hex


def parse_invitation_fragment(fragment: str) -> tuple[str | None, str]:
    """Parse a viewer invitation URL fragment → ``(channel_pub_hex, bearer)``.

    Accepts the two-value grammar ``k=<channel_pub>&t=<bearer>`` (complete) and
    the legacy bearer-only ``t=<bearer>`` (returns ``channel_pub_hex is None``).
    A legacy fragment is reported honestly rather than treated as complete; the
    caller decides whether a keyless link is acceptable. Any other shape —
    extra keys, duplicates, a missing bearer — is malformed and raises.
    """
    try:
        parsed = urllib.parse.parse_qs(
            fragment, keep_blank_values=True, strict_parsing=True,
        )
    except ValueError as exc:
        raise InvitationError("invitation fragment is malformed") from exc
    keys = set(parsed)
    if keys not in ({INVITE_FRAGMENT_BEARER},
                    {INVITE_FRAGMENT_CHANNEL_KEY, INVITE_FRAGMENT_BEARER}):
        raise InvitationError(
            "invitation fragment must carry a bearer 't' and optionally a "
            "channel key 'k' — nothing else"
        )
    if len(parsed[INVITE_FRAGMENT_BEARER]) != 1:
        raise InvitationError("invitation fragment carries a duplicate bearer")
    bearer = parsed[INVITE_FRAGMENT_BEARER][0]
    if not _HEX64.fullmatch(bearer):
        raise InvitationError(
            "invitation fragment bearer must be 64 lowercase hex characters"
        )
    channel_pub_hex = None
    if INVITE_FRAGMENT_CHANNEL_KEY in parsed:
        values = parsed[INVITE_FRAGMENT_CHANNEL_KEY]
        if len(values) != 1:
            raise InvitationError(
                "invitation fragment carries a duplicate channel key")
        channel_pub_hex = decode_channel_pub(values[0])
    return channel_pub_hex, bearer


@dataclass(frozen=True)
class InvitationJoinUrl:
    """The result of assembling a complete viewer invitation URL.

    ``complete`` links carry BOTH fragment values (``k`` and ``t``) and expose
    the full URL in ``url``. When either value is absent the result is an
    explicit incomplete/legacy marker: ``url`` is ``None`` and ``reason`` names
    the missing value. No surface ever emits a bearer-only URL from an
    incomplete result.
    """

    complete: bool
    url: str | None
    reason: str | None = None


def _require_canonical_join_url(canonical_url: object) -> str:
    """The registry-minted canonical URL an invitation fragment attaches to.

    It must be a bare ``https`` link with NO query, NO fragment, and no
    credentials — the fragment values live only in the URL we build here and
    never in the stored/canonical URL. A structurally invalid URL is a registry
    or caller bug and raises loudly rather than yielding a plausible link.
    """
    if not isinstance(canonical_url, str) or not canonical_url:
        raise InvitationError("invitation canonical URL is missing")
    parsed = urllib.parse.urlsplit(canonical_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise InvitationError("invitation canonical URL is malformed")
    return canonical_url


def build_invitation_join_url(
    canonical_url: str,
    channel_pub_hex: object,
    bearer: object,
) -> InvitationJoinUrl:
    """The one shared serializer for a viewer invitation URL (graph://4f9e881c-a9 §3).

    Attaches the two independent fragment values to the canonical URL as
    ``#k=<channel_pub>&t=<bearer>``. Both are read from the organization-homed
    grant — ``channel_pub`` from ``NetworkLinkGrantV6``, the separately retained
    ``bearer`` — so no private channel seed is ever opened to build a viewer URL.

    Returns an explicit incomplete/legacy result when either value is absent;
    never a bearer-only URL, never ``root_pub`` in place of ``k``. Every
    invitation-producing surface (publication result, Membership Copy/Share,
    CLI, email, QR, API projections) consumes this so they all emit the SAME
    complete URL.
    """
    url = _require_canonical_join_url(canonical_url)
    if not isinstance(channel_pub_hex, str) or not channel_pub_hex:
        return InvitationJoinUrl(
            complete=False, url=None,
            reason="channel-verification key is absent (legacy or keyless link)",
        )
    if bearer is None or bearer == "":
        return InvitationJoinUrl(
            complete=False, url=None,
            reason="invitation bearer is absent (minted before bearers were retained)",
        )
    if not isinstance(bearer, str) or not _HEX64.fullmatch(bearer):
        raise InvitationError(
            "invitation bearer must be 64 lowercase hex characters"
        )
    fragment = (
        f"{INVITE_FRAGMENT_CHANNEL_KEY}={encode_channel_pub(channel_pub_hex)}"
        f"&{INVITE_FRAGMENT_BEARER}="
        f"{urllib.parse.quote(bearer, safe='')}"
    )
    return InvitationJoinUrl(complete=True, url=f"{url}#{fragment}", reason=None)


def invitation_from_join_url(
    *,
    org: str,
    root_pub: str,
    invite_ref: str,
    join_url: str,
) -> Invitation:
    """Assemble an invitation v2 from a minted ``org:join`` URL.

    The registry grant comes from ``/l/<grant>`` in the request path. The
    ledger bearer comes from the client-only fragment — the two-value
    ``#k=<channel_pub>&t=<bearer>`` grammar (graph://4f9e881c-a9 §3) or the
    legacy bearer-only ``#t=<bearer>``. Parsing those positions here keeps the
    domain split identical in the CLI minter and the B6 harness driver. The
    channel PUBLIC key, when present, authenticates the browser viewer's
    serving handshake and is not part of the container-side ``AUTONOMY_INVITE``
    credential set, so it is validated and dropped here.
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
    _channel_pub_hex, bearer = parse_invitation_fragment(parsed.fragment)
    channel_token, claim_token = _require_tokens(parts[-1], bearer)
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
