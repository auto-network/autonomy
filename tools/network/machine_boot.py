"""Joining-machine state for the fleet enrollment ceremony.

First boot creates no durable machine identity.  It produces an ephemeral
enrollment request and the shared verification code shown to the operator.
Only after approval, roster commit, and root delivery does the machine accept
the assigned durable id and store it in machine.db.  Its operating key remains
derived from ``personal_root + machine_id`` and is never stored separately.
"""

from __future__ import annotations

from tools.network import fleet_enroll, fleet_roster, fleet_route
from tools.network.fleet_enroll import EnrollmentDelivery, EnrollmentRequest
from tools.network.fleet_invite import FleetInvite
from tools.network.idkit import KeyPair, derive_machine_key
from tools.network.idkit import canonical_json
from tools.network.idkit.errors import IdkitError
from tools.network.idkit.keys import verify_signature


class MachineBootError(RuntimeError):
    """The machine identity could not be created, read, or accepted."""


FLEET_COMPLETION_DOMAIN = b"autonomy.fleet.enrollment-completion.v1\n"


def completion_input(*, request_id: str, roster_entry_id: str) -> bytes:
    return FLEET_COMPLETION_DOMAIN + canonical_json({
        "v": 1,
        "request_id": request_id,
        "roster_entry_id": roster_entry_id,
    })


def has_identity(*, org="machine") -> bool:
    """Whether this installation has completed fleet enrollment."""
    return machine_id(org=org) is not None


def machine_id(*, org="machine") -> str | None:
    row = _read_row(org=org)
    return row["machine_id"] if row is not None else None


def first_boot(invite: FleetInvite, *, org="machine") -> tuple[EnrollmentRequest, str]:
    """Start a one-time ceremony, marking this machine as joining a fleet.

    Writes no durable machine *identity* — that is assigned only after approval
    — but does record the durable ``fleet-joining`` marker, so the machine fails
    CLOSED on tunnel serving from this instant (see ``mark_joining``).
    """
    if has_identity(org=org):
        raise MachineBootError(
            "this machine is already enrolled; do not start another first boot"
        )
    mark_joining(invite, org=org)
    request = fleet_enroll.build_request(invite=invite)
    return request, fleet_enroll.verification_code(request)


def complete_enrollment(
    delivery: EnrollmentDelivery,
    request: EnrollmentRequest,
    personal_root_seed: bytes,
    *,
    invite: FleetInvite,
    channel_binding: str,
    org="machine",
) -> KeyPair:
    """Verify the bound approval, store its assigned id, and return its key."""
    if has_identity(org=org):
        raise MachineBootError("this machine is already enrolled")
    try:
        assigned_id, key = fleet_enroll.verify_delivery(
            delivery,
            request,
            invite=invite,
            channel_binding=channel_binding,
            personal_root_seed=personal_root_seed,
        )
    except (ValueError, TypeError, IdkitError) as exc:
        raise MachineBootError(f"could not accept fleet approval: {exc}") from exc
    for entry in delivery.roster_entries:
        fleet_roster.store_entry(entry, org=None)
    _write_row({"machine_id": assigned_id}, org=org)
    return key


def accept_browser_completion(
    delivery: EnrollmentDelivery,
    request: EnrollmentRequest,
    *,
    invite: FleetInvite,
    channel_binding: str,
    request_id: str,
    machine_id_value: str,
    proof: str,
    org="machine",
) -> None:
    """Persist the id after the browser proves the delivered machine key.

    The server verifies public root-signed evidence and the machine-key proof;
    the personal root seed remains in the browser that derived that key.
    """
    if has_identity(org=org):
        raise MachineBootError("this machine is already enrolled")
    try:
        fleet_enroll.verify_approval(
            delivery.approval,
            request,
            invite=invite,
            channel_binding=channel_binding,
            roster_entry=delivery.roster_entry,
            anchor_root_pub=invite.personal_root_pub,
        )
        if machine_id_value != delivery.roster_entry.machine_id:
            raise ValueError("completion names a different machine id")
        if request_id != fleet_enroll.request_id(request):
            raise ValueError("completion names a different enrollment request")
        verify_signature(
            delivery.roster_entry.machine_pub,
            proof,
            completion_input(
                request_id=request_id,
                roster_entry_id=delivery.roster_entry.entry_id,
            ),
        )
        bootstrap = fleet_enroll.verify_bootstrap_roster(
            delivery,
            anchor_root_pub=invite.personal_root_pub,
            joining_machine_pub=delivery.roster_entry.machine_pub,
        )
    except (ValueError, TypeError, IdkitError) as exc:
        raise MachineBootError(
            f"could not accept browser fleet completion: {exc}"
        ) from exc
    remote = tuple(
        entry for entry in bootstrap
        if entry.machine_pub != delivery.roster_entry.machine_pub
    )
    if len(remote) != 1:
        raise MachineBootError(
            "initial Fleet delivery must identify exactly one origin route"
        )
    # Each public record self-verifies against the personal root. Store this
    # minimum first-peer snapshot and its machine-local route before adopting
    # the local identity; a crash can leave harmless authorization evidence
    # but never an identity unable to authenticate the origin peer.
    for entry in bootstrap:
        fleet_roster.store_entry(entry, org=None)
    fleet_route.store(
        fleet_route.FleetRoute(invite.rendezvous, remote[0].machine_pub),
        org=org,
    )
    _write_row({"machine_id": machine_id_value}, org=org)


def accept_local_bootstrap(
    entry,
    *,
    anchor_root_pub: str,
    org="machine",
) -> None:
    """Adopt the root-holder Dashboard as the first roster member."""
    from tools.network import fleet_roster, fleet_tunnel_server

    fleet_roster.verify(entry, anchor_root_pub=anchor_root_pub)
    if entry.kind != fleet_roster.EntryKind.ENROLL:
        raise MachineBootError("local Fleet bootstrap must be an enrollment")
    if entry.assignment != fleet_roster.FLEET_MEMBER_ASSIGNMENT:
        raise MachineBootError(
            "local Fleet bootstrap must grant personal_root_holder standing"
        )
    existing = machine_id(org=org)
    if existing is not None:
        if existing != entry.machine_id:
            raise MachineBootError(
                "this Dashboard already has a different Fleet identity"
            )
        return
    if fleet_roster.load_entries(org=None):
        raise MachineBootError(
            "local Fleet bootstrap is only valid before the first roster entry"
        )
    fleet_roster.store_entry(entry, org=None)
    _write_row({"machine_id": entry.machine_id}, org=org)
    fleet_tunnel_server.select(
        entry.machine_id, anchor_root_pub=anchor_root_pub
    )


def operating_key(personal_root_seed: bytes, *, org="machine") -> KeyPair:
    """Re-derive this enrolled machine's operating key on demand."""
    mid = machine_id(org=org)
    if mid is None:
        raise MachineBootError("this machine is not enrolled")
    return derive_machine_key(personal_root_seed, mid)


_SET_ID = "autonomy.machine.identity"
_KEY = "self"


def _write_row(payload: dict, *, org) -> None:
    from tools.graph import settings_ops
    from tools.graph.schemas.machine_identity import MACHINE_IDENTITY_REVISION

    settings_ops.add_setting(
        _SET_ID, MACHINE_IDENTITY_REVISION, _KEY, payload, org=org, state="raw"
    )


def _read_row(*, org) -> dict | None:
    from tools.graph import settings_ops
    from tools.graph.schemas.machine_identity import MACHINE_IDENTITY_REVISION

    members = settings_ops.read_owned_set(
        _SET_ID, org=org, target_revision=MACHINE_IDENTITY_REVISION
    ).to_dict()
    member = members.get(_KEY)
    return member.payload if member is not None else None


_JOINING_SET_ID = "autonomy.machine.fleet-joining"
_JOINING_KEY = "self"


def mark_joining(invite: FleetInvite, *, org="machine") -> None:
    """Record that this machine has begun joining a fleet.

    Written at invite presentation, before any network round-trip, so the
    machine fails CLOSED on tunnel serving from that instant even if the
    ceremony never finishes or the process restarts mid-flight.

    upsert, not add: first_boot may run more than once (a declined or expired
    ceremony is retried), so re-presenting an invite must rewrite the marker
    rather than violate the settings uniqueness constraint.
    """
    from datetime import datetime, timezone

    from tools.graph import settings_ops
    from tools.graph.schemas.fleet_joining import FLEET_JOINING_REVISION

    settings_ops.upsert_by_key(
        _JOINING_SET_ID,
        FLEET_JOINING_REVISION,
        _JOINING_KEY,
        {
            "invite_id": invite.invite_id,
            "since": datetime.now(timezone.utc).isoformat(),
        },
        org=org,
        state="raw",
    )


def is_joining(*, org="machine") -> bool:
    """Whether this machine has begun a fleet-join ceremony."""
    from tools.graph import settings_ops
    from tools.graph.schemas.fleet_joining import FLEET_JOINING_REVISION

    members = settings_ops.read_owned_set(
        _JOINING_SET_ID, org=org, target_revision=FLEET_JOINING_REVISION
    ).to_dict()
    return members.get(_JOINING_KEY) is not None


def mark_joining_from_env(*, org="machine") -> bool:
    """Mark this machine as fleet-joining if it was booted with a fleet invite.

    Reads ``AUTONOMY_FLEET_INVITE``. When it is set and this machine is not yet
    enrolled, write the joining marker so the node fails CLOSED on tunnel
    serving from the first moment it comes up — before the enrollment ceremony
    runs and before any ``machine_id`` exists. A no-op when the variable is
    unset or the machine is already enrolled. Returns whether a marker landed.

    A malformed code still marks joining (under a sentinel id): a node booted to
    join a fleet must never sit serving as a legacy primary because its invite
    failed to parse; the parse failure surfaces separately at enrollment.
    """
    import os

    code = os.environ.get("AUTONOMY_FLEET_INVITE")
    if not code or has_identity(org=org):
        return False

    from tools.network import fleet_invite

    try:
        invite = fleet_invite.decode(code)
    except Exception:
        invite = FleetInvite(
            personal_root_pub="", rendezvous="",
            invite_id="unparsed-fleet-invite", expires_at=0, signature="",
        )
    mark_joining(invite, org=org)
    return True
