"""Joining-machine state for the fleet enrollment ceremony.

First boot creates no durable machine identity.  It produces an ephemeral
enrollment request and the shared verification code shown to the operator.
Only after approval, roster commit, and root delivery does the machine accept
the assigned durable id and store it in machine.db.  Its operating key remains
derived from ``personal_root + machine_id`` and is never stored separately.
"""

from __future__ import annotations

from tools.network import fleet_enroll
from tools.network.fleet_enroll import EnrollmentDelivery, EnrollmentRequest
from tools.network.fleet_invite import FleetInvite
from tools.network.idkit import KeyPair, derive_machine_key


class MachineBootError(RuntimeError):
    """The machine identity could not be created, read, or accepted."""


def has_identity(*, org="machine") -> bool:
    """Whether this installation has completed fleet enrollment."""
    return machine_id(org=org) is not None


def machine_id(*, org="machine") -> str | None:
    row = _read_row(org=org)
    return row["machine_id"] if row is not None else None


def first_boot(invite: FleetInvite, *, org="machine") -> tuple[EnrollmentRequest, str]:
    """Start a one-time ceremony without writing durable machine state."""
    if has_identity(org=org):
        raise MachineBootError(
            "this machine is already enrolled; do not start another first boot"
        )
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
    except (ValueError, TypeError) as exc:
        raise MachineBootError(f"could not accept fleet approval: {exc}") from exc
    _write_row({"machine_id": assigned_id}, org=org)
    return key


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
