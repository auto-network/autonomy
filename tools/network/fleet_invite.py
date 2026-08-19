"""The fleet-machine invite — one personal-root-signed link (auto-ei8w0).

A fleet is the set of machines holding one operator's PERSONAL root
(design ``graph://0c655045-ee4``). This is the link, minted on the primary,
that invites a new machine into that set. It is a NEW invite kind beside the
organization invitation (:mod:`tools.network.invitation`), carried on the
same install surface — the demo starts from an empty system and one link, so
enrolment must ride the surface a person already uses.

Three properties the design turns on:

* **Anchored on, and signed by, the PERSONAL root — never an organization
  root.** An organization root authenticates organization membership, which
  is neither necessary nor sufficient for "this machine is mine". The
  accepting machine verifies the offer against the personal root public key
  the offer carries, and that same key is the fleet anchor it will later
  check the sealed-root delivery against (``auto-gx2mt`` piece 3).

* **It carries nothing that lets its holder act as the operator.** The
  personal root PRIVATE key is never in the link; the link holds only public
  material (the anchor public key, a rendezvous, a correlation id) plus a
  signature. A holder cannot produce anything the roster would accept,
  because minting a roster entry needs the personal root private key.

* **It is a bearer credential ONLY in the window before acceptance, and only
  to REQUEST.** A stolen link lets an attacker ask to enrol — which the
  operator then sees and declines (``auto-5ydhe``). It never lets them
  complete an enrolment.

The rendezvous is how a machine that has never spoken to the fleet reaches
the primary dashboard, and how the reply returns; the offer names it, and
``auto-b6fee`` drives the request back through it. This module mints,
encodes, decodes and verifies the offer — it does not itself perform the
rendezvous.

``mint`` is a pure function taking the personal root, and it runs in the
primary's BROWSER during sign-on (the dashboard never holds the personal
root — auto-a1pub); ``decode``/``verify`` run on the accepting machine.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field

from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit.errors import IdkitError, MalformedError, SignatureError
from tools.network.idkit.keys import verify_signature

#: Domain separator — a signature minted for any other purpose (an org
#: invite, a ledger event, a cert) can never verify as a fleet offer, and
#: the version is frozen: changing it invalidates every prior offer.
FLEET_INVITE_DOMAIN = b"autonomy.network.fleet-invite.v1\n"
FLEET_INVITE_VERSION = 1
#: The offer's kind, carried in the body so a decoder can refuse an
#: organization invite fed to this path before it does any crypto.
FLEET_INVITE_KIND = "fleet-machine"

_CHECKSUM_CHARS = 8
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")  # an Ed25519 signature


class FleetInviteError(ValueError):
    """The fleet invite is malformed, truncated, not a fleet invite, or does
    not verify against its stated anchor."""


@dataclass(frozen=True)
class FleetInvite:
    """A decoded, not-yet-verified fleet-machine offer.

    Every field is PUBLIC. There is deliberately no secret here — the value
    of the link is the personal root's signature over these public facts,
    not any bearer token it carries. ``invite_id`` correlates the enrolment
    request that returns through the rendezvous; it is a nonce, not a
    credential.
    """

    personal_root_pub: str  # 64-hex — the fleet anchor and the signing key
    rendezvous: str         # https URL the new machine reaches the primary at
    invite_id: str          # 64-hex correlation nonce
    expires_at: int         # unix ms; 0 means no expiry
    signature: str = field(repr=False)  # personal-root sig over the body


def _body(
    *, personal_root_pub: str, rendezvous: str, invite_id: str, expires_at: int
) -> dict:
    """The exact, ordering-independent object the signature covers. The
    signature is NOT in it — canonical_json sorts keys, so signer and
    verifier build identical bytes."""
    return {
        "v": FLEET_INVITE_VERSION,
        "kind": FLEET_INVITE_KIND,
        "personal_root_pub": personal_root_pub,
        "rendezvous": rendezvous,
        "invite_id": invite_id,
        "expires_at": int(expires_at),
    }


def signing_input(
    *, personal_root_pub: str, rendezvous: str, invite_id: str, expires_at: int
) -> bytes:
    return FLEET_INVITE_DOMAIN + canonical_json(
        _body(
            personal_root_pub=personal_root_pub, rendezvous=rendezvous,
            invite_id=invite_id, expires_at=expires_at,
        )
    )


def _require_hex64(value, what: str) -> str:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise FleetInviteError(f"fleet invite {what} must be 64 lowercase hex chars")
    return value


def _require_rendezvous(value) -> str:
    if not isinstance(value, str) or not value:
        raise FleetInviteError("fleet invite rendezvous must be a non-empty string")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise FleetInviteError("fleet invite rendezvous must be an https URL")
    return value


def mint(
    personal_root: KeyPair,
    *,
    rendezvous: str,
    invite_id: str,
    expires_at: int = 0,
) -> FleetInvite:
    """Sign a fleet offer with the operator's PERSONAL root.

    Runs where the personal root actually is — the primary's BROWSER, during
    sign-on, while the seed is live and before the ceremony zeroes it. It
    CANNOT run server-side: the personal root seed is on the dashboard's
    never-hold list (auto-a1pub / crib §12), so nothing on the server can
    sign AS the personal root — even though the dashboard does hold a
    bounded, attenuated agent-delegate signing key for other work, which is
    not this. A dashboard-side handler at most ACCEPTS an already-signed
    offer minted here; it never calls this itself.

    ``personal_root``'s public half becomes both the carried anchor and the
    key the signature verifies against — the offer is self-describing and
    self-authenticating, and a machine adopts that anchor when it accepts.
    Nothing secret leaves in the result.
    """
    root_pub = _require_hex64(personal_root.public_hex, "personal_root_pub")
    rendezvous = _require_rendezvous(rendezvous)
    invite_id = _require_hex64(invite_id, "invite_id")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool) or expires_at < 0:
        raise FleetInviteError("fleet invite expires_at must be a non-negative int")
    sig = personal_root.sign_hex(
        signing_input(
            personal_root_pub=root_pub, rendezvous=rendezvous,
            invite_id=invite_id, expires_at=expires_at,
        )
    )
    return FleetInvite(
        personal_root_pub=root_pub, rendezvous=rendezvous, invite_id=invite_id,
        expires_at=expires_at, signature=sig,
    )


def encode(invite: FleetInvite) -> str:
    """Render the link an operator carries into the installation
    (``AUTONOMY_FLEET_INVITE=…``). Body plus a short checksum so a truncated
    or mis-pasted link fails cleanly before any crypto runs."""
    body = _body(
        personal_root_pub=invite.personal_root_pub, rendezvous=invite.rendezvous,
        invite_id=invite.invite_id, expires_at=invite.expires_at,
    )
    body["sig"] = invite.signature
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    checksum = hashlib.sha256(raw).hexdigest()[:_CHECKSUM_CHARS]
    return f"{payload}.{checksum}"


def decode(code: str) -> FleetInvite:
    """Parse and structurally validate a link — NO signature check yet (that
    is :func:`verify`, which needs to decide the anchor). Refuses a
    truncated link (checksum), a non-fleet code (kind), and any malformed
    field, and never raises anything but :class:`FleetInviteError`."""
    if not isinstance(code, str) or code.count(".") != 1:
        raise FleetInviteError("fleet invite code is malformed")
    payload, checksum = code.split(".")
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except (ValueError, binascii.Error):
        raise FleetInviteError("fleet invite code is not valid base64") from None
    if hashlib.sha256(raw).hexdigest()[:_CHECKSUM_CHARS] != checksum:
        raise FleetInviteError("fleet invite code is truncated or corrupt")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise FleetInviteError("fleet invite body is not valid JSON") from None
    if not isinstance(data, dict):
        raise FleetInviteError("fleet invite body is not an object")
    if data.get("v") != FLEET_INVITE_VERSION:
        raise FleetInviteError(
            f"unsupported fleet invite version {data.get('v')!r}"
        )
    if data.get("kind") != FLEET_INVITE_KIND:
        # An organization invite (or anything else) fed to this path is
        # refused before any crypto — the kinds must not be confusable.
        raise FleetInviteError(
            f"not a fleet-machine invite (kind={data.get('kind')!r})"
        )
    sig = data.get("sig")
    if not isinstance(sig, str) or not _HEX128.match(sig):
        raise FleetInviteError("fleet invite signature is malformed")
    expires = data.get("expires_at")
    if not isinstance(expires, int) or isinstance(expires, bool) or expires < 0:
        raise FleetInviteError("fleet invite expires_at is malformed")
    return FleetInvite(
        personal_root_pub=_require_hex64(data.get("personal_root_pub"),
                                         "personal_root_pub"),
        rendezvous=_require_rendezvous(data.get("rendezvous")),
        invite_id=_require_hex64(data.get("invite_id"), "invite_id"),
        expires_at=expires,
        signature=sig,
    )


def verify(invite: FleetInvite, *, expected_root_pub: str | None = None) -> None:
    """Check the offer's signature against the PERSONAL root it names, and —
    on a machine that already holds a root — that the anchor is the operator's
    own.

    The signature verifies under ``personal_root_pub`` and nothing else: an
    offer signed by an organization root, or by any other key, FAILS here,
    so the anchor kind cannot be incidentally satisfied by both. A fresh
    machine (``expected_root_pub=None``) accepts any self-consistent offer
    and adopts its anchor; a machine already in a fleet passes its own root
    and an offer for a DIFFERENT personal root is rejected with the anchor
    named. Raises :class:`FleetInviteError` on any failure.
    """
    if expected_root_pub is not None:
        expected = _require_hex64(expected_root_pub, "expected_root_pub")
        if invite.personal_root_pub != expected:
            raise FleetInviteError(
                "fleet invite anchor "
                f"{invite.personal_root_pub[:12]}… is not this machine's "
                f"personal root {expected[:12]}… — refusing a second fleet"
            )
    try:
        verify_signature(
            invite.personal_root_pub,
            invite.signature,
            signing_input(
                personal_root_pub=invite.personal_root_pub,
                rendezvous=invite.rendezvous,
                invite_id=invite.invite_id,
                expires_at=invite.expires_at,
            ),
        )
    except SignatureError as exc:
        raise FleetInviteError(
            "fleet invite does not verify against its stated personal root"
        ) from exc
    except IdkitError as exc:
        raise FleetInviteError(f"fleet invite anchor is unusable: {exc}") from exc
