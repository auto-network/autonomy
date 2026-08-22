"""This machine has begun joining a fleet (auto-clune.7 fail-closed marker).

Written the instant a fleet invite is presented — at the top of
``machine_boot.first_boot``, before any network round-trip and before the
enrollment ceremony assigns a durable ``machine_id``. Stored in machine.db
(``@home("machine")``, never synced), so it survives a restart mid-ceremony
and travels in a copied data volume.

Its only reader is ``fleet_tunnel_server.state()``: a machine that has begun
joining is a Fleet member from that instant, so it must fail CLOSED on tunnel
serving (never ``legacy-unmanaged``) until its roster arrives and — only if the
operator later elects it — a selection names it. A genuine standalone install
never presents a fleet invite, so it never carries this marker and still reads
``legacy-unmanaged``.

This is the durable, in-code replacement for the ``AUTONOMY_DISABLE_TUNNEL_SERVING``
operator kill-switch: it fails closed for exactly the copied/partially-enrolled
population that switch existed to catch, without depending on an env override.
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

FLEET_JOINING_SET_ID = "autonomy.machine.fleet-joining"
FLEET_JOINING_REVISION = 1
FLEET_JOINING_KEY = "self"

SYNOPSIS = {
    "summary": (
        "Marker that this machine has begun joining a fleet, written at invite "
        "presentation and stored in machine.db (never synced). Read by "
        "fleet_tunnel_server.state() to fail closed on tunnel serving before the "
        "roster arrives. See tools.network.machine_boot."
    ),
    "nouns": [
        "fleet joining", "fleet member provisioning", "tunnel serving fail-closed",
    ],
    "related_set_ids": ["autonomy.machine.identity#1", "autonomy.fleet.roster#2"],
}


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="machine_self")
class FleetJoiningV1(SettingSchema):
    """The one row saying this machine has begun a fleet-join ceremony."""

    set_id = FLEET_JOINING_SET_ID
    schema_revision = FLEET_JOINING_REVISION

    invite_id: str = field(
        required=True,
        description="Id of the fleet invite this machine began joining under.",
    )
    since: str = field(
        required=False,
        description="ISO-8601 timestamp the join began. Provenance only.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        v = payload.get("invite_id")
        if not isinstance(v, str) or not v:
            raise SchemaValidationError(
                f"{cls.__name__}: 'invite_id' must be a non-empty string"
            )
