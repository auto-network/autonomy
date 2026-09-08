"""``autonomy.fleet.diagnostic-target#1`` — how to reach a machine to collect
diagnostics from it.

Which machines exist, how to reach them, and what command runs Python in the
right place there is state the operator expects to see again and that more than
one session needs. That makes it a Settings set rather than a constant in a
script or a string pasted between sessions — which is how it was carried until
now, and why every cross-machine check tonight began with somebody re-asking
for an ssh string.

Homed on the operator's personal store: the machines are theirs, and the set
follows them across the fleet rather than living on one computer. It carries no
credential — an ssh destination names a key path, it does not contain one.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    SettingSchema,
    SchemaValidationError,
    home,
    publication_band,
)

FLEET_DIAGNOSTIC_TARGET_SET_ID = "autonomy.fleet.diagnostic-target"
FLEET_DIAGNOSTIC_TARGET_REVISION = 1

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


@publication_band(min="raw", max="curated")
@home("personal")
class FleetDiagnosticTargetV1(SettingSchema):
    """One machine a diagnostic can be collected from."""

    set_id = FLEET_DIAGNOSTIC_TARGET_SET_ID
    schema_revision = FLEET_DIAGNOSTIC_TARGET_REVISION

    _field_metadata: dict[str, dict] = {
        "ssh": {
            "type": "string", "required": False,
            "description": (
                "Destination exactly as `ssh` would take it, flags folded in "
                "(e.g. '-i ~/.ssh/sjc -p 11222 root@<tailnet-ip>'). Omit for "
                "the machine the command runs on."
            ),
        },
        "remote_cmd": {
            "type": "string", "required": False,
            "description": (
                "Command prefix that runs Python where the live data is. A "
                "containerized node needs 'docker exec -u autonomy "
                "autonomy-dashboard-1 python3'; a host venv seat may point at "
                "a stale pre-cutover data directory and report a healthy node "
                "as unenrolled, so name the container explicitly."
            ),
        },
        "role": {
            "type": "string", "required": False,
            "description": (
                "What this machine is in the fleet: 'home', 'fleet-member' "
                "(another machine of the same operator), or 'org-member' (a "
                "machine of a DIFFERENT member of the organization). The "
                "org-member role is what distinguishes fleet sync from "
                "member-to-member org sync in a report."
            ),
        },
        "note": {"type": "string", "required": False, "description": "Free text."},
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, got {type(payload).__name__}"
            )
        for field in ("ssh", "remote_cmd", "role", "note"):
            value = payload.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 512):
                raise SchemaValidationError(
                    f"{cls.__name__}: {field!r} must be a string under 512 chars"
                )
        role = payload.get("role")
        if role is not None and role not in ("home", "fleet-member", "org-member"):
            raise SchemaValidationError(
                f"{cls.__name__}: 'role' must be home, fleet-member or org-member"
            )
        if payload.get("ssh") and not payload.get("remote_cmd"):
            raise SchemaValidationError(
                f"{cls.__name__}: a remote target needs 'remote_cmd' — the "
                "command that runs Python where the live data actually is"
            )

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        if not _NAME_RE.match(key or ""):
            raise SchemaValidationError(
                f"{cls.__name__}: keys are short machine names like 'sjc-2', got {key!r}"
            )
