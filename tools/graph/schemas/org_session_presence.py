"""Which of a member's sessions are live on which of their machines, for the
organization (auto-qrmlg.8, graph://bace7454-c77 "The directory: how it is
maintained").

The organization-homed counterpart of ``autonomy.personal.session-presence``:
one row per live session of THIS organization, keyed
``<org serving machine_pub>:<tmux_name>``, written only by the machine the
session runs on, only when something about it changes, deprecated (never
deleted) when it ends. The row names the member whose session it is
(``persona_pub``); the envelope every organization row carries (signed
settings) is signed by that member's delegate and verified at every
receiving boundary, so a stored row is a verified statement by that member.

It is not a heartbeat. A reader decides liveness from whether the machine is
present in the organization's live relay serving slots or was pulled from
recently (tools/dashboard/session_presence.py). Raw band, never
follow-visible: co-members see it, followers and other organizations do not.
"""

from __future__ import annotations

import re
from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)

ORG_SESSION_PRESENCE_SET_ID = "autonomy.org.session-presence"
ORG_SESSION_PRESENCE_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Live sessions of this organization on each member machine, one row "
        "per session keyed machine_pub:tmux_name and naming the member whose "
        "session it is; written by the machine it runs on when something "
        "changes and deprecated when it ends. Lets co-members list, and "
        "later reach, each other's sessions."
    ),
    "nouns": [
        "session presence", "session roster", "member session",
        "session directory", "organization",
    ],
    "related_set_ids": [
        "autonomy.personal.session-presence#1",
        "autonomy.org.fleet-reachability#1",
    ],
}

_HEX64 = re.compile(r"[0-9a-f]{64}")
STATES = ("LAUNCHING", "ACTIVE", "STOPPING")


@home("organization")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="machine_pub:tmux_name")
class OrgSessionPresenceV1(SettingSchema):
    set_id = ORG_SESSION_PRESENCE_SET_ID
    schema_revision = ORG_SESSION_PRESENCE_REVISION

    persona_pub: str = field(
        required=True,
        description=(
            "The member persona whose session this is (64 hex); the row's "
            "signer resolves to this persona at every receiving boundary"
        ),
    )
    machine_id: str = field(
        required=False,
        description="Durable fleet machine id of the machine, when the member's fleet names one",
    )
    label: str = field(required=False, description="Session working title, if one is set")
    project: str = field(required=False, description="Workspace id")
    type: str = field(required=False, description="Session type (container, host, ...)")
    harness: str = field(required=False, description="Running CLI harness")
    model: str = field(required=False, description="Most recent model id")
    role: str = field(required=False, description="Session role, if set")
    state: str = field(
        required=True, description="Lifecycle state: LAUNCHING, ACTIVE or STOPPING",
    )
    since: int = field(required=True, description="Unix seconds the session was created")
    launched_by: str = field(
        required=False, description="Who started it; set by a remote launch",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        if not _HEX64.fullmatch(str(payload.get("persona_pub", ""))):
            raise SchemaValidationError(f"{cls.__name__}: 'persona_pub' must be 64 lowercase hex")
        machine_id = payload.get("machine_id")
        if machine_id is not None and not _HEX64.fullmatch(str(machine_id)):
            raise SchemaValidationError(f"{cls.__name__}: 'machine_id' must be 64 lowercase hex")
        if payload.get("state") not in STATES:
            raise SchemaValidationError(f"{cls.__name__}: 'state' must be one of {list(STATES)}")
