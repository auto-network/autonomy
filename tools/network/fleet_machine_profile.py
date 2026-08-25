"""Personal-scoped human labels for authorized Fleet machines."""

from __future__ import annotations

from tools.graph import settings_ops
from tools.graph.schemas.fleet_machine_profile import (
    FLEET_MACHINE_PROFILE_REVISION,
    FLEET_MACHINE_PROFILE_SET_ID,
    FleetMachineProfileV1,
)


def normalize_display_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("machine name must be text")
    name = value.strip()
    FleetMachineProfileV1.validate({"display_name": name})
    return name


def store(machine_id: str, display_name: object) -> None:
    name = normalize_display_name(display_name)
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            FLEET_MACHINE_PROFILE_SET_ID,
            FLEET_MACHINE_PROFILE_REVISION,
            machine_id,
            {"display_name": name},
            org=None,
        )


def names(*, org=None) -> dict[str, str]:
    members = settings_ops.read_owned_set(
        FLEET_MACHINE_PROFILE_SET_ID,
        org=org,
        target_revision=FLEET_MACHINE_PROFILE_REVISION,
    ).members
    result: dict[str, str] = {}
    for member in members:
        payload = member.payload
        try:
            FleetMachineProfileV1.validate(payload)
        except Exception:
            continue
        machine_id = str(member.key)
        if len(machine_id) != 64 or any(char not in "0123456789abcdef" for char in machine_id):
            continue
        result[machine_id] = payload["display_name"]
    return result
