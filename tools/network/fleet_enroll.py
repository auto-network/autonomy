"""One-time fleet-enrollment ceremony and roster authorization.

The invitation is machine-neutral.  A joining installation contributes only
an ephemeral ``enrollment_nonce`` so the two dashboards can display the same
short authentication string (SAS).  That nonce is *not* a machine identity
and is discarded when the ceremony completes.

After the operator compares the SAS, trusted browser code opens the personal
root, deterministically assigns the durable machine id, derives its key, and
signs two domain-separated records.  The server receives no root secret: it
verifies the durable roster entry and transient request/channel approval
against the protected personal-root public anchor, commits the roster first,
and only then enables delivery of the unchanged armor.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from tools.network import fleet_roster
from tools.network.fleet_invite import FleetInvite
from tools.network.idkit import KeyPair, canonical_json, derive_machine_key
from tools.network.idkit.errors import IdkitError
from tools.network.idkit.keys import verify_signature

FLEET_ENROLL_VERSION = 2
FLEET_ENROLL_SAS_DOMAIN = b"autonomy.network.fleet-enroll-sas.v2\n"
FLEET_MACHINE_ID_DOMAIN = b"autonomy.network.fleet-machine-id.v1\n"
FLEET_APPROVAL_DOMAIN = b"autonomy.fleet.enrollment-approval.v1\n"
FLEET_APPROVAL_VERSION = 1
FLEET_REQUEST_ID_DOMAIN = b"autonomy.fleet.enrollment-request.v1\n"


class FleetEnrollError(ValueError):
    """An enrollment request or approval is malformed or mismatched."""


@dataclass(frozen=True)
class EnrollmentRequest:
    """A request carried by one encrypted invitation channel.

    Every field is public.  ``enrollment_nonce`` exists only for this
    ceremony; it neither authorizes the machine nor survives enrollment.
    """

    enrollment_nonce: str
    personal_root_pub: str
    invite_id: str

    def to_dict(self) -> dict:
        return {
            "enrollment_nonce": self.enrollment_nonce,
            "personal_root_pub": self.personal_root_pub,
            "invite_id": self.invite_id,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "EnrollmentRequest":
        expected = {"enrollment_nonce", "personal_root_pub", "invite_id"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise FleetEnrollError(
                "fleet enrollment request must carry exactly enrollment_nonce, "
                "personal_root_pub, and invite_id"
            )
        return cls(
            enrollment_nonce=_require_hex64(
                payload["enrollment_nonce"], "enrollment_nonce"
            ),
            personal_root_pub=_require_hex64(
                payload["personal_root_pub"], "personal_root_pub"
            ),
            invite_id=_require_hex64(payload["invite_id"], "invite_id"),
        )


@dataclass(frozen=True)
class EnrollmentApproval:
    """Transient browser-root-signed binding for one exact delivery channel."""

    version: int
    enrollment_nonce: str
    personal_root_pub: str
    invite_id: str
    channel_binding: str
    roster_entry_id: str
    signature: str

    def binding_dict(self) -> dict:
        return {
            "v": self.version,
            "enrollment_nonce": self.enrollment_nonce,
            "personal_root_pub": self.personal_root_pub,
            "invite_id": self.invite_id,
            "channel_binding": self.channel_binding,
            "roster_entry_id": self.roster_entry_id,
        }

    def signing_input(self) -> bytes:
        return FLEET_APPROVAL_DOMAIN + canonical_json(self.binding_dict())

    def to_dict(self) -> dict:
        return {**self.binding_dict(), "signature": self.signature}

    @classmethod
    def from_dict(cls, payload: dict) -> "EnrollmentApproval":
        expected = {
            "v", "enrollment_nonce", "personal_root_pub", "invite_id",
            "channel_binding", "roster_entry_id", "signature",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise FleetEnrollError(
                "fleet enrollment approval must carry exactly the signed fields"
            )
        return cls(
            version=payload["v"],
            enrollment_nonce=payload["enrollment_nonce"],
            personal_root_pub=payload["personal_root_pub"],
            invite_id=payload["invite_id"],
            channel_binding=payload["channel_binding"],
            roster_entry_id=payload["roster_entry_id"],
            signature=payload["signature"],
        )


@dataclass(frozen=True)
class EnrollmentDelivery:
    """Public signed evidence returned only after durable roster commit."""

    approval: EnrollmentApproval
    roster_entry: fleet_roster.RosterEntry


def build_request(
    *, invite: FleetInvite, enrollment_nonce: str | None = None
) -> EnrollmentRequest:
    """Create the ephemeral request for one invitation channel."""
    nonce = enrollment_nonce or os.urandom(32).hex()
    return EnrollmentRequest(
        enrollment_nonce=_require_hex64(nonce, "enrollment_nonce"),
        personal_root_pub=_require_hex64(
            invite.personal_root_pub, "personal_root_pub"
        ),
        invite_id=_require_hex64(invite.invite_id, "invite_id"),
    )


def verify_request(request: EnrollmentRequest, *, invite: FleetInvite) -> None:
    """Validate that a request belongs to the invitation carrying it."""
    _require_hex64(request.enrollment_nonce, "enrollment_nonce")
    if request.invite_id != invite.invite_id:
        raise FleetEnrollError("enrollment request is for a different invite")
    if request.personal_root_pub != invite.personal_root_pub:
        raise FleetEnrollError("enrollment request names a different fleet anchor")


def verification_code(request: EnrollmentRequest) -> str:
    """Return the shared SAS rendered on the old and new dashboards.

    It covers the complete public request rather than only the nonce, so the
    operator's comparison binds the fleet anchor and invitation correlation as
    well as the exact joining channel's challenge.
    """
    body = canonical_json({
        "v": FLEET_ENROLL_VERSION,
        "enrollment_nonce": _require_hex64(
            request.enrollment_nonce, "enrollment_nonce"
        ),
        "personal_root_pub": _require_hex64(
            request.personal_root_pub, "personal_root_pub"
        ),
        "invite_id": _require_hex64(request.invite_id, "invite_id"),
    })
    digest = hashlib.sha256(FLEET_ENROLL_SAS_DOMAIN + body).hexdigest().upper()
    return " ".join(digest[i:i + 4] for i in range(0, 24, 4))


def request_id(request: EnrollmentRequest) -> str:
    """Content id for one complete, machine-neutral enrollment request."""
    frozen = EnrollmentRequest.from_dict(request.to_dict())
    return hashlib.sha256(
        FLEET_REQUEST_ID_DOMAIN + canonical_json(frozen.to_dict())
    ).hexdigest()


def assigned_machine_id(
    personal_root_seed: bytes, request: EnrollmentRequest
) -> str:
    """Derive the durable id assigned *after* this request is approved.

    The result is pseudorandom under the personal root and deterministic for
    retries of the same approved request.  It is separate from the ephemeral
    nonce used for the human comparison.
    """
    if not isinstance(personal_root_seed, bytes) or len(personal_root_seed) != 32:
        raise FleetEnrollError("personal_root_seed must be exactly 32 raw bytes")
    material = canonical_json({
        "v": FLEET_ENROLL_VERSION,
        "enrollment_nonce": _require_hex64(
            request.enrollment_nonce, "enrollment_nonce"
        ),
        "personal_root_pub": _require_hex64(
            request.personal_root_pub, "personal_root_pub"
        ),
        "invite_id": _require_hex64(request.invite_id, "invite_id"),
    })
    return hmac.new(
        personal_root_seed, FLEET_MACHINE_ID_DOMAIN + material, hashlib.sha256
    ).hexdigest()


def approval_draft(
    request: EnrollmentRequest,
    *,
    invite: FleetInvite,
    channel_binding: str,
    roster_entry: fleet_roster.RosterEntry,
) -> EnrollmentApproval:
    """Build the exact transient record the browser must root-sign.

    This function handles public data only.  The browser signs
    :meth:`EnrollmentApproval.signing_input`; the server never receives the
    root key used to do so.
    """
    verify_request(request, invite=invite)
    return EnrollmentApproval(
        version=FLEET_APPROVAL_VERSION,
        enrollment_nonce=request.enrollment_nonce,
        personal_root_pub=request.personal_root_pub,
        invite_id=request.invite_id,
        channel_binding=_require_hex64(channel_binding, "channel_binding"),
        roster_entry_id=_require_hex64(
            roster_entry.entry_id, "roster_entry_id"
        ),
        signature="0" * 128,
    )


def authorize_request(
    approval: EnrollmentApproval,
    request: EnrollmentRequest,
    *,
    invite: FleetInvite,
    channel_binding: str,
    roster_entry: fleet_roster.RosterEntry,
    anchor_root_pub: str,
    org=None,
) -> EnrollmentDelivery:
    """Verify browser evidence and durably authorize before delivery.

    ``anchor_root_pub`` must be resolved by the caller from the protected
    ``autonomy.identity.personal`` row.  It is deliberately not inferred from
    either submitted record.  Returning a delivery is the transport handoff,
    so a failed store produces no deliverable result.
    """
    verify_approval(
        approval,
        request,
        invite=invite,
        channel_binding=channel_binding,
        roster_entry=roster_entry,
        anchor_root_pub=anchor_root_pub,
    )
    fleet_roster.store_entry(roster_entry, org=org)
    return EnrollmentDelivery(approval=approval, roster_entry=roster_entry)


def verify_approval(
    approval: EnrollmentApproval,
    request: EnrollmentRequest,
    *,
    invite: FleetInvite,
    channel_binding: str,
    roster_entry: fleet_roster.RosterEntry,
    anchor_root_pub: str,
) -> None:
    """Verify one browser-signed approval against server-owned context."""
    verify_request(request, invite=invite)
    if isinstance(approval.version, bool) or not isinstance(approval.version, int) \
            or approval.version != FLEET_APPROVAL_VERSION:
        raise FleetEnrollError(
            f"unsupported fleet enrollment approval version: {approval.version!r}"
        )
    if approval.enrollment_nonce != request.enrollment_nonce:
        raise FleetEnrollError("approval is for a different enrollment ceremony")
    if approval.personal_root_pub != request.personal_root_pub:
        raise FleetEnrollError("approval is for a different fleet anchor")
    if approval.invite_id != request.invite_id:
        raise FleetEnrollError("approval is for a different invite")
    expected_channel = _require_hex64(channel_binding, "channel_binding")
    if approval.channel_binding != expected_channel:
        raise FleetEnrollError("approval is for a different invitation channel")
    anchor = _require_hex64(anchor_root_pub, "anchor_root_pub")
    if request.personal_root_pub != anchor:
        raise FleetEnrollError("request does not match the stored personal root")
    if approval.roster_entry_id != roster_entry.entry_id:
        raise FleetEnrollError("approval names a different roster entry")
    fleet_roster.verify(roster_entry, anchor_root_pub=anchor)
    if roster_entry.kind != fleet_roster.EntryKind.ENROLL:
        raise FleetEnrollError("approval roster entry is not an enrollment")
    if roster_entry.assignment != fleet_roster.FLEET_MEMBER_ASSIGNMENT:
        raise FleetEnrollError("approval roster entry grants the wrong standing")
    _require_hex128(approval.signature, "signature")
    try:
        verify_signature(anchor, approval.signature, approval.signing_input())
    except IdkitError as exc:
        raise FleetEnrollError(
            "fleet enrollment approval does not verify against the personal root"
        ) from exc


def verify_delivery(
    delivery: EnrollmentDelivery,
    request: EnrollmentRequest,
    *,
    invite: FleetInvite,
    channel_binding: str,
    personal_root_seed: bytes,
) -> tuple[str, KeyPair]:
    """Verify delivered evidence and return the local id and operating key."""
    if not isinstance(personal_root_seed, bytes) or len(personal_root_seed) != 32:
        raise FleetEnrollError("personal_root_seed must be exactly 32 raw bytes")
    root = KeyPair.from_private_hex(personal_root_seed.hex())
    if root.public_hex != request.personal_root_pub:
        raise FleetEnrollError("delivered root does not match the invited fleet")
    verify_approval(
        delivery.approval,
        request,
        invite=invite,
        channel_binding=channel_binding,
        roster_entry=delivery.roster_entry,
        anchor_root_pub=root.public_hex,
    )
    machine_id = assigned_machine_id(personal_root_seed, request)
    key = derive_machine_key(personal_root_seed, machine_id)
    if delivery.roster_entry.machine_id != machine_id:
        raise FleetEnrollError("approval carries the wrong durable machine id")
    if delivery.roster_entry.machine_pub != key.public_hex:
        raise FleetEnrollError("approval carries the wrong machine public key")
    return machine_id, key


def _require_hex64(value, what: str) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise FleetEnrollError(
            f"fleet enroll {what} must be 64 lowercase hex chars"
        )
    return value


def _require_hex128(value, what: str) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{128}", value):
        raise FleetEnrollError(
            f"fleet enroll {what} must be 128 lowercase hex chars"
        )
    return value
