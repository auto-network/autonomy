"""Machine-local route to an enrolled Fleet peer.

The first alpha reuses the invitation's already-published RelayKit channel as
the durable route credential after admission. The route is local bootstrap
state, never personal data: it must not replicate to another machine because
each machine receives and can later rotate its own route.
"""

from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)

FLEET_ROUTE_SET_ID = "autonomy.machine.fleet-route"
FLEET_ROUTE_REVISION = 1
FLEET_ROUTE_KEY = "origin"

SYNOPSIS = {
    "summary": (
        "This installation's machine-local RelayKit route to its first Fleet "
        "peer. It contains a bearer URL and never synchronizes."
    ),
    "nouns": ["fleet route", "fleet peer route", "fleet relay credential"],
    "related_set_ids": ["autonomy.machine.identity#1"],
}

_HEX = "0123456789abcdef"


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="fleet_route_role")
class FleetRouteV1(SettingSchema):
    set_id = FLEET_ROUTE_SET_ID
    schema_revision = FLEET_ROUTE_REVISION

    rendezvous: str = field(
        required=True,
        description="Exact HTTPS /l/<token> route; bearer-bearing and local only.",
    )
    origin_machine_pub: str = field(
        required=True,
        description="64-hex roster key expected at the other end.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        rendezvous = payload.get("rendezvous")
        if (
            not isinstance(rendezvous, str)
            or not rendezvous.startswith("https://")
            or "/l/" not in rendezvous
            or len(rendezvous) > 2048
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'rendezvous' must be a bounded HTTPS link route"
            )
        public = payload.get("origin_machine_pub")
        if (
            not isinstance(public, str)
            or len(public) != 64
            or any(char not in _HEX for char in public)
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'origin_machine_pub' must be 64 lowercase hex chars"
            )
