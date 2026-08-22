"""Human labels for Fleet machines, separate from signed authorization.

The parent operator supplies the display name during admission. It is plain
personal-scoped metadata keyed by durable machine id; roster authority stays
entirely in the root-signed ``autonomy.fleet.roster`` record.
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

FLEET_MACHINE_PROFILE_SET_ID = "autonomy.fleet.machine-profile"
FLEET_MACHINE_PROFILE_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Human-supplied Fleet machine names, keyed by durable machine id. "
        "Display metadata only; signed authority remains in the Fleet roster."
    ),
    "nouns": ["fleet machine name", "machine label", "fleet machine profile"],
    "related_set_ids": ["autonomy.fleet.roster#2"],
}

_HEX = "0123456789abcdef"


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="machine_id")
class FleetMachineProfileV1(SettingSchema):
    set_id = FLEET_MACHINE_PROFILE_SET_ID
    schema_revision = FLEET_MACHINE_PROFILE_REVISION

    machine_id: str = field(
        required=True,
        description="64-hex durable Fleet machine id this label describes.",
    )
    display_name: str = field(
        required=True,
        description=(
            "Human-supplied machine name collected by the parent approval "
            "dialogue. Display metadata, not Fleet authority."
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
        name = payload.get("display_name")
        if (
            not isinstance(name, str)
            or name != name.strip()
            or not name
            or len(name) > 80
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'display_name' must be 1-80 trimmed characters "
                "without ASCII controls"
            )
