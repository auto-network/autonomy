"""Roster-based election of one machine for singular-ownership duties.

**This module no longer decides who may serve.** ``auto-clune.7`` activated at
46eed6d5: every authorized Fleet machine runs its own connector, and
:func:`tunnel_serving_permitted` is the serving predicate. A machine is
refused only when it is mid-join, holds no personal root, or is absent from
the active roster.

What ``state()`` and its ``allowed`` field still answer is which single
machine owns the duties that must not run concurrently across a fleet:
credential refresh, usage-row maintenance, and enrollment targeting. Reading
``allowed`` as "may this machine serve" is the defect that made a dead
connector read as expected; ask :func:`tunnel_serving_permitted` instead.

Renaming the Setting and its reason values to match this narrower meaning is
tracked separately as ``auto-2fz3b``.
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
        # A machine handed a fleet invite has begun joining; its enrollment
        # ceremony may not have written a machine_id yet (or ever, if it
        # restarts first, or if it is a copied data volume). Fail closed on the
        # durable joining marker so it never serves as a second primary before
        # its roster arrives. Only a genuine standalone install — which never
        # presents a fleet invite — falls through to legacy-unmanaged.
        try:
            joining = machine_boot.is_joining(org="machine")
        except Exception:
            joining = True  # an unreadable machine store must fail closed
        if joining:
            return TunnelServerState(
                False,
                True,
                "fleet-member-provisioning",
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

#: Reasons that withhold ``allowed`` for DESIGNATION alone. Everything else in
#: :func:`state` withholds it for a safety reason and keeps withholding it.
_DESIGNATION_ONLY = frozenset({"not-designated", "tunnel-server-unassigned"})


def tunnel_serving_permitted() -> tuple[bool, str]:
    """May THIS machine run a serving connector? -> ``(permitted, reason)``.

    Narrower than :func:`state`, and deliberately additive: ``state()`` and its
    ``allowed`` field are untouched, because they are the fleet's SINGULAR
    OWNERSHIP election and several consumers that have nothing to do with
    tunnels depend on exactly one machine answering True — credential refresh
    (claude and codex), usage-row maintenance, enrollment targeting. Widening
    ``allowed`` to enable tunnels would let every machine refresh a
    single-use credential concurrently.

    Only two of ``state()``'s reasons are about designation. This permits those
    and nothing else, so a machine that is mid-join, holds no personal root, or
    has an unreadable, empty or invalid roster still may not serve.

    ONE OF THE TWO NEEDS EXTRA VALIDATION. ``not-designated`` is returned only
    AFTER ``state()`` has checked that this machine has a durable identity and
    is in the active roster, so it is safe on its own. ``tunnel-server-unassigned``
    is returned BEFORE either check, so permitting it directly would let a
    machine with no identity, or one absent from the roster, start serving. It
    is therefore re-validated here against the same two conditions.
    """
    current = state()
    if current.allowed:
        return True, current.reason
    if current.reason not in _DESIGNATION_ONLY:
        return False, current.reason
    if current.reason == "not-designated":
        return True, "designation-not-required"

    # tunnel-server-unassigned: establish local identity and roster membership,
    # which state() had not yet reached when it returned.
    try:
        local = machine_boot.machine_id(org="machine")
    except Exception:
        return False, "machine-identity-unreadable"
    if local is None:
        return False, "machine-identity-missing"
    root_pub = _personal_root_pub()
    if root_pub is None:
        return False, "personal-root-missing"
    try:
        entries = fleet_roster.load_entries(org=None)
        active = {
            entry.machine_id
            for entry in fleet_roster.resolve(entries, anchor_root_pub=root_pub).values()
        }
    except Exception:
        return False, "roster-unreadable"
    if local not in active:
        return False, "machine-not-rostered"
    return True, "designation-not-required"
