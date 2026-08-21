"""Temporary Fleet assignment for singular-registry tunnel serving.

Production relay ownership is currently one tunnel per organization.  Until
``auto-0rc9f`` replaces that with cooperative pools, one active Fleet machine
is selected to run this person's serving connectors.  This is operational
configuration, not a privileged or "primary" machine role.

The row is personal-scoped and raw so the personal synchronization engine
carries it to every Fleet node without exposing Fleet topology to an
organization-scoped view.  ``auto-clune.7`` deletes this entire schema after
cooperative pools pass their two-connector acceptance.
"""

from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    publication_band,
    singleton,
)

FLEET_TUNNEL_SERVER_SET_ID = "autonomy.fleet.tunnel-server"
FLEET_TUNNEL_SERVER_REVISION = 1
FLEET_TUNNEL_SERVER_KEY = "default"

SYNOPSIS = {
    "summary": (
        "Temporary selection of the active Fleet machine that serves "
        "auto.network tunnels while the registry supports only one tunnel "
        "per organization. Removed by auto-clune.7 after cooperative pools."
    ),
    "nouns": [
        "fleet tunnel server", "designated tunnel machine",
        "selected serving machine", "singular tunnel compatibility",
    ],
    "related_set_ids": [
        "autonomy.fleet.roster#2", "autonomy.machine.identity#1",
    ],
}

_HEX = "0123456789abcdef"


@home("personal")
@publication_band(max="raw")
@singleton(key=FLEET_TUNNEL_SERVER_KEY)
class FleetTunnelServerV1(SettingSchema):
    """The active roster machine temporarily selected for tunnel serving."""

    set_id = FLEET_TUNNEL_SERVER_SET_ID
    schema_revision = FLEET_TUNNEL_SERVER_REVISION

    machine_id: str = field(
        required=True,
        description=(
            "64-hex durable machine id of an active root-signed Fleet roster "
            "entry. This is an operational selection, not Fleet authority."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        machine_id = payload.get("machine_id")
        if (
            not isinstance(machine_id, str)
            or len(machine_id) != 64
            or any(char not in _HEX for char in machine_id)
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'machine_id' must be exactly 64 lowercase hex chars"
            )
