"""Temporary roster-based gate for singular auto.network tunnel ownership.

This module deliberately has one job: while the registry still replaces an
organization's prior tunnel, determine whether THIS Fleet machine may run the
serving connector.  It does not elect a leader and it grants no authority.
The synced personal Setting names one already-authorized roster machine.

``auto-clune.7`` removes this module and its schema when cooperative tunnel
pools ship.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re

from tools.graph import settings_ops
from tools.graph.schemas.fleet_tunnel_server import (
    FLEET_TUNNEL_SERVER_KEY,
    FLEET_TUNNEL_SERVER_REVISION,
    FLEET_TUNNEL_SERVER_SET_ID,
)
from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
from tools.network import fleet_roster, machine_boot

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class FleetTunnelServerError(ValueError):
    """A tunnel-server assignment could not be trusted or stored."""


@dataclass(frozen=True)
class TunnelServerState:
    """One Dashboard's eligibility to run serving connectors.

    ``managed`` is false only for installations that have not initialized the
    Fleet model at all.  That migration state preserves legacy single-node
    serving. Once any roster entry exists, uncertainty fails closed.
    """

    allowed: bool
    managed: bool
    reason: str
    selected_machine_id: str | None = None
    local_machine_id: str | None = None
    active_machine_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _personal_root_pub() -> str | None:
    """Resolve the trusted Fleet anchor from personal identity only."""
    try:
        members = settings_ops.read_owned_set(
            PERSONAL_IDENTITY_SET_ID, org=None
        ).members
    except Exception:
        return None
    candidates = [
        member for member in members
        if isinstance(member.payload, dict)
    ]
    if not candidates:
        return None
    member = next(
        (candidate for candidate in candidates if candidate.key == "default"),
        sorted(candidates, key=lambda candidate: candidate.key)[0],
    )
    root_pub = member.payload.get("root_pub")
    if isinstance(root_pub, str) and _HEX64.fullmatch(root_pub):
        return root_pub
    armor = member.payload.get("armored_private_key")
    if not isinstance(armor, str):
        return None
    try:
        from tools.network.idkit.armor import armor_root_pub

        return armor_root_pub(armor)
    except Exception:
        return None


def _stored_selection() -> tuple[str | None, str | None]:
    """Return ``(machine_id, error_reason)`` from the personal singleton."""
    try:
        members = settings_ops.read_owned_set(
            FLEET_TUNNEL_SERVER_SET_ID,
            org=None,
            target_revision=FLEET_TUNNEL_SERVER_REVISION,
        ).members
    except Exception:
        return None, "assignment-unreadable"
    rows = [member for member in members if member.key == FLEET_TUNNEL_SERVER_KEY]
    if not rows:
        return None, None
    if len(rows) != 1 or not isinstance(rows[0].payload, dict):
        return None, "assignment-invalid"
    machine_id = rows[0].payload.get("machine_id")
    if not isinstance(machine_id, str) or _HEX64.fullmatch(machine_id) is None:
        return None, "assignment-invalid"
    return machine_id, None


def state() -> TunnelServerState:
    """Return whether this installation may own auto.network connectors.

    No roster rows means the Fleet model has not been initialized and the old
    single-Dashboard behavior remains intact.  Once rows exist, all failures
    are managed, visible, and fail closed.
    """
    selected, selection_error = _stored_selection()
    try:
        local = machine_boot.machine_id(org="machine")
    except Exception:
        local = None
    try:
        entries = fleet_roster.load_entries(org=None)
    except Exception:
        return TunnelServerState(
            False,
            True,
            "roster-unreadable",
            selected_machine_id=selected,
            local_machine_id=local,
        )
    if not entries:
        # A synced assignment or a durable local machine id proves that Fleet
        # initialization has begun.  Do not mistake partial sync for a legacy
        # install and start a competing tunnel before its roster arrives.
        if selection_error is not None:
            return TunnelServerState(
                False,
                True,
                selection_error,
                local_machine_id=local,
            )
        if selected is not None or local is not None:
            return TunnelServerState(
                False,
                True,
                "roster-empty",
                selected_machine_id=selected,
                local_machine_id=local,
            )
        return TunnelServerState(True, False, "legacy-unmanaged")

    root_pub = _personal_root_pub()
    if root_pub is None:
        return TunnelServerState(False, True, "personal-root-missing")
    try:
        roster = fleet_roster.resolve(entries, anchor_root_pub=root_pub)
    except Exception:
        return TunnelServerState(False, True, "roster-unreadable")
    active = {entry.machine_id: entry for entry in roster.values()}
    if not active:
        return TunnelServerState(False, True, "roster-empty")
    if len(active) != len(roster):
        return TunnelServerState(
            False, True, "roster-invalid", active_machine_count=len(active)
        )

    if selection_error is not None:
        return TunnelServerState(
            False, True, selection_error, active_machine_count=len(active)
        )
    if selected is None:
        if len(active) != 1:
            return TunnelServerState(
                False,
                True,
                "tunnel-server-unassigned",
                active_machine_count=len(active),
            )
        selected = next(iter(active))
        selection_reason = "single-member-implicit"
    else:
        selection_reason = "selected"
        if selected not in active:
            return TunnelServerState(
                False,
                True,
                "assignment-inactive",
                selected_machine_id=selected,
                active_machine_count=len(active),
            )

    if local is None:
        return TunnelServerState(
            False,
            True,
            "machine-identity-missing",
            selected_machine_id=selected,
            active_machine_count=len(active),
        )
    if local not in active:
        return TunnelServerState(
            False,
            True,
            "machine-not-rostered",
            selected_machine_id=selected,
            local_machine_id=local,
            active_machine_count=len(active),
        )
    if local != selected:
        return TunnelServerState(
            False,
            True,
            "not-designated",
            selected_machine_id=selected,
            local_machine_id=local,
            active_machine_count=len(active),
        )
    return TunnelServerState(
        True,
        True,
        selection_reason,
        selected_machine_id=selected,
        local_machine_id=local,
        active_machine_count=len(active),
    )


def select(machine_id: str, *, anchor_root_pub: str | None = None) -> str:
    """Select an active roster machine and return the stored Setting id."""
    if not isinstance(machine_id, str) or _HEX64.fullmatch(machine_id) is None:
        raise FleetTunnelServerError(
            "selected machine_id must be exactly 64 lowercase hex chars"
        )
    try:
        entries = fleet_roster.load_entries(org=None)
    except Exception as exc:
        raise FleetTunnelServerError("fleet roster is unreadable") from exc
    root_pub = anchor_root_pub or _personal_root_pub()
    if root_pub is None:
        raise FleetTunnelServerError("personal root is unavailable")
    if not isinstance(root_pub, str) or _HEX64.fullmatch(root_pub) is None:
        raise FleetTunnelServerError("personal root is invalid")
    roster = fleet_roster.resolve(entries, anchor_root_pub=root_pub)
    active_ids = {entry.machine_id for entry in roster.values()}
    if len(active_ids) != len(roster):
        raise FleetTunnelServerError("fleet roster has duplicate machine ids")
    if machine_id not in active_ids:
        raise FleetTunnelServerError(
            "selected machine is not active in the trusted Fleet roster"
        )
    return settings_ops.upsert_by_key(
        FLEET_TUNNEL_SERVER_SET_ID,
        FLEET_TUNNEL_SERVER_REVISION,
        FLEET_TUNNEL_SERVER_KEY,
        {"machine_id": machine_id},
        org=None,
        state="raw",
    )


def preserve_single_member_selection(
    *, anchor_root_pub: str | None = None,
) -> str | None:
    """Freeze an implicit one-member choice before the roster grows.

    Enrollment calls this after verifying approval evidence and before adding
    the next roster entry.  Without it, the second committed member would turn
    an implicit single-member choice into an unassigned multi-member roster
    and stop serving everywhere. Concurrent growth from one member writes the
    same singleton value, so the operation is idempotent.

    Returns the selected machine id, or ``None`` when there is no existing
    member to preserve. An already explicit selection is returned unchanged.
    A multi-member unassigned roster remains unassigned and fails closed; this
    helper never invents a winner after ambiguity already exists.
    """
    selected, selection_error = _stored_selection()
    if selection_error is not None:
        raise FleetTunnelServerError(selection_error)
    if selected is not None:
        return selected
    try:
        entries = fleet_roster.load_entries(org=None)
    except Exception as exc:
        raise FleetTunnelServerError("fleet roster is unreadable") from exc
    if not entries:
        return None
    root_pub = anchor_root_pub or _personal_root_pub()
    if not isinstance(root_pub, str) or _HEX64.fullmatch(root_pub) is None:
        root_pub = None
    if root_pub is None:
        raise FleetTunnelServerError("personal root is unavailable")
    roster = fleet_roster.resolve(entries, anchor_root_pub=root_pub)
    active_ids = {entry.machine_id for entry in roster.values()}
    if len(active_ids) != len(roster):
        raise FleetTunnelServerError("fleet roster has duplicate machine ids")
    if len(active_ids) != 1:
        return None
    selected = next(iter(active_ids))
    settings_ops.upsert_by_key(
        FLEET_TUNNEL_SERVER_SET_ID,
        FLEET_TUNNEL_SERVER_REVISION,
        FLEET_TUNNEL_SERVER_KEY,
        {"machine_id": selected},
        org=None,
        state="raw",
    )
    return selected
