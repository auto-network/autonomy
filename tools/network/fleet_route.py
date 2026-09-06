"""Machine-local Fleet peer route accepted at enrollment completion."""

from __future__ import annotations

from dataclasses import dataclass

from tools.graph import settings_ops
from tools.graph.schemas.fleet_route import (
    FLEET_ROUTE_KEY,
    FLEET_ROUTE_REVISION,
    FLEET_ROUTE_SET_ID,
    FleetRouteV1,
)


#: Link target types a fleet sync pull may ride. ``fleet:join`` is the
#: invitation (bootstrap: how a machine that has never spoken to the fleet
#: reaches it; it expires). ``fleet:sync`` is a machine's STANDING sync
#: address: minted by the serving machine itself, never expiring, granting
#: nothing on its own -- every request on it still passes the
#: roster-authenticated machine handshake, and enrollment refuses it.
FLEET_JOIN_TARGET_TYPE = "fleet:join"
FLEET_ROUTE_TARGET_TYPE = "fleet:sync"
FLEET_SYNC_TARGET_TYPES = frozenset({FLEET_JOIN_TARGET_TYPE, FLEET_ROUTE_TARGET_TYPE})


@dataclass(frozen=True)
class FleetRoute:
    rendezvous: str
    origin_machine_pub: str


#: Row roles. ``origin`` is the route this machine PULLS from (its first
#: peer; the invitation at enrollment, rotated to that peer's standing route
#: once discovered). ``self`` is this machine's OWN standing route -- the
#: fleet:sync link it serves on and announces to roster peers -- so a
#: restart re-announces the same address instead of minting another.
ORIGIN_ROLE = FLEET_ROUTE_KEY
SELF_ROLE = "self"


def store(route: FleetRoute, *, org="machine", role: str = ORIGIN_ROLE) -> None:
    payload = {
        "rendezvous": route.rendezvous,
        "origin_machine_pub": route.origin_machine_pub,
    }
    FleetRouteV1.validate(payload)
    settings_ops.upsert_by_key(
        FLEET_ROUTE_SET_ID,
        FLEET_ROUTE_REVISION,
        role,
        payload,
        org=org,
    )


def load(*, org="machine", role: str = ORIGIN_ROLE) -> FleetRoute | None:
    members = settings_ops.read_owned_set(
        FLEET_ROUTE_SET_ID,
        org=org,
        target_revision=FLEET_ROUTE_REVISION,
    ).to_dict()
    member = members.get(role)
    if member is None:
        return None
    FleetRouteV1.validate(member.payload)
    return FleetRoute(**member.payload)


def load_self(*, org="machine") -> FleetRoute | None:
    """This machine's own standing route (``origin_machine_pub`` is its own key)."""
    return load(org=org, role=SELF_ROLE)


def store_self(route: FleetRoute, *, org="machine") -> None:
    store(route, org=org, role=SELF_ROLE)
