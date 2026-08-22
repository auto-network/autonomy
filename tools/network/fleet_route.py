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


@dataclass(frozen=True)
class FleetRoute:
    rendezvous: str
    origin_machine_pub: str


def store(route: FleetRoute, *, org="machine") -> None:
    payload = {
        "rendezvous": route.rendezvous,
        "origin_machine_pub": route.origin_machine_pub,
    }
    FleetRouteV1.validate(payload)
    settings_ops.upsert_by_key(
        FLEET_ROUTE_SET_ID,
        FLEET_ROUTE_REVISION,
        FLEET_ROUTE_KEY,
        payload,
        org=org,
    )


def load(*, org="machine") -> FleetRoute | None:
    members = settings_ops.read_owned_set(
        FLEET_ROUTE_SET_ID,
        org=org,
        target_revision=FLEET_ROUTE_REVISION,
    ).to_dict()
    member = members.get(FLEET_ROUTE_KEY)
    if member is None:
        return None
    FleetRouteV1.validate(member.payload)
    return FleetRoute(**member.payload)
