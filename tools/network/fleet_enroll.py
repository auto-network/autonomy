"""First boot on a fleet link: the machine key and the ask-to-join (auto-b6fee).

Design ``graph://0c655045-ee4``, piece 2 of the fleet epic. An empty install,
handed a fleet invite (:mod:`tools.network.fleet_invite`), generates its OWN
keypair — the private half never leaves the machine, and nothing else ever
produces it — renders a fingerprint for the operator to compare against what
the primary shows, and sends an enrollment request back to the primary.

Three properties the design turns on:

* **The machine generates its own key; the private half stays in the machine
  store.** ``machine.db`` replicates nowhere — not even across the operator's
  own fleet — because a machine key that synced would let any fleet machine
  impersonate any other, inverting the property the roster provides. This
  module produces the key and the request; the vault-sealed placement in the
  machine store, always unlocked at first boot (crib §10, corrected: no
  no-unlock disk variant), is wired by the caller and reviewed as crypto's
  domain.

* **The fingerprint is the operator's only defense against approving the
  wrong machine.** The primary shows a fingerprint; the machine shows a
  fingerprint; the operator compares them before approving. So both sides
  MUST render the same key in the same form — a format difference silently
  defeats the check. :func:`fingerprint` is that one shared rendering.

* **Before approval the machine holds nothing of the fleet's.** The
  enrollment request carries only the machine's PUBLIC key plus a
  proof-of-possession and the invite correlation. It grants the holder
  nothing: a stolen request lets an attacker be *offered* enrolment of a key
  they cannot use (they lack its private half), which the operator then
  declines. The machine adopts no personal root until the approval seals one
  to it (auto-5ydhe).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit.errors import IdkitError, SignatureError
from tools.network.idkit.keys import verify_signature
from tools.network.fleet_invite import FleetInvite

#: Domain separator for the enrollment request's proof of possession — a
#: machine signature minted for anything else cannot verify as a join proof.
FLEET_ENROLL_DOMAIN = b"autonomy.network.fleet-enroll.v1\n"
FLEET_ENROLL_VERSION = 1


class FleetEnrollError(ValueError):
    """An enrollment request is malformed or its proof does not verify."""


def fingerprint(machine_pub: str) -> str:
    """The ONE human-comparable rendering of a machine key, shared by the
    primary and the joining machine. Both sides call this; a divergence would
    defeat the operator's compare-before-approve check, so it is defined once.

    Form: the SHA-256 of the raw public key, rendered as six space-separated
    groups of four uppercase hex — short enough to read aloud and compare, and
    over the HASH rather than the key itself so it is fixed-width regardless of
    key encoding. Deterministic and pure.
    """
    _require_hex64(machine_pub, "machine_pub")
    digest = hashlib.sha256(bytes.fromhex(machine_pub)).hexdigest().upper()
    groups = [digest[i:i + 4] for i in range(0, 24, 4)]
    return " ".join(groups)


@dataclass(frozen=True)
class EnrollmentRequest:
    """A machine's ask-to-join, sent back to the primary through the invite's
    rendezvous. Every field is PUBLIC; the value is the proof, not any bearer.
    """

    machine_pub: str          # the machine's own authorization public key
    personal_root_pub: str    # the fleet anchor, from the invite
    invite_id: str            # correlates to the invite the primary minted
    proof: str = field(repr=False)  # machine sig — proof of possession + consent


def _proof_input(*, machine_pub: str, personal_root_pub: str, invite_id: str) -> bytes:
    return FLEET_ENROLL_DOMAIN + canonical_json({
        "v": FLEET_ENROLL_VERSION,
        "machine_pub": machine_pub,
        "personal_root_pub": personal_root_pub,
        "invite_id": invite_id,
    })


def build_request(machine_key: KeyPair, *, invite: FleetInvite) -> EnrollmentRequest:
    """The joining machine builds its request. Signs, with its OWN key, a
    proof binding {this machine key, this fleet's anchor, this invite} — so
    the primary learns the machine holds the private half (possession) and
    consents to join THIS fleet via THIS invite. A request built for one fleet
    or one invite does not verify for another."""
    machine_pub = _require_hex64(machine_key.public_hex, "machine_pub")
    root_pub = _require_hex64(invite.personal_root_pub, "personal_root_pub")
    invite_id = _require_hex64(invite.invite_id, "invite_id")
    proof = machine_key.sign_hex(
        _proof_input(machine_pub=machine_pub, personal_root_pub=root_pub,
                     invite_id=invite_id)
    )
    return EnrollmentRequest(
        machine_pub=machine_pub, personal_root_pub=root_pub,
        invite_id=invite_id, proof=proof,
    )


def verify_request(request: EnrollmentRequest, *, invite: FleetInvite) -> None:
    """The primary verifies a returned request against the invite it minted.

    Checks the request references THIS invite (id + anchor) and that its proof
    verifies against the machine's OWN key — proof of possession. A request
    that names another invite, another fleet, or whose proof does not verify
    is refused, so a replayed or forged request cannot enrol a key its sender
    does not hold. Raises :class:`FleetEnrollError` on any failure.
    """
    if request.invite_id != invite.invite_id:
        raise FleetEnrollError("enrollment request is for a different invite")
    if request.personal_root_pub != invite.personal_root_pub:
        raise FleetEnrollError("enrollment request names a different fleet anchor")
    try:
        verify_signature(
            request.machine_pub,
            request.proof,
            _proof_input(
                machine_pub=request.machine_pub,
                personal_root_pub=request.personal_root_pub,
                invite_id=request.invite_id,
            ),
        )
    except SignatureError as exc:
        raise FleetEnrollError(
            "enrollment proof does not verify against the machine key "
            "(the sender does not hold its private half)"
        ) from exc
    except IdkitError as exc:
        raise FleetEnrollError(f"enrollment request machine key is unusable: {exc}") from exc


def _require_hex64(value, what: str) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise FleetEnrollError(f"fleet enroll {what} must be 64 lowercase hex chars")
    return value
