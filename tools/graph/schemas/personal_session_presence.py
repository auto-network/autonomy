"""Which sessions are live on which of the operator's own machines.

One row per live session, keyed ``<machine_pub>:<tmux_name>`` and written only
by the machine the session runs on, only when something about it changes
(graph://7eb29bc8-31a §9.5). A session that ends is deprecated, not deleted:
the deprecation is the tombstone that replicates. Personal-homed, so ordinary
fleet sync carries it to every machine in the roster; the roster admission that
guards fleet sync is its trust boundary, and it carries no signature of its own.

It is not a heartbeat. A row says what its machine last reported; a reader
decides liveness from whether that machine is reachable now
(tools/dashboard/session_presence.py). The organization-homed counterpart
(auto-qrmlg.8) uses the same writer with an envelope-signed sink.
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

PERSONAL_SESSION_PRESENCE_SET_ID = "autonomy.personal.session-presence"
PERSONAL_SESSION_PRESENCE_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Live sessions on each of the operator's own machines, one row per "
        "session keyed machine_pub:tmux_name, written by the machine it runs "
        "on when something changes and deprecated when it ends. Lets every "
        "fleet machine list, and later reach, sessions running elsewhere."
    ),
    "nouns": [
        "session presence", "remote session", "session directory",
        "machine", "personal fleet",
    ],
    "related_set_ids": [
        "autonomy.personal.fleet-reachability#2",
        "autonomy.fleet.machine-profile#1",
    ],
}

_HEX64 = re.compile(r"[0-9a-f]{64}")
#: A tmux session name as the launcher mints it; also bounds the key.
_TMUX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
STATES = ("LAUNCHING", "ACTIVE", "STOPPING")


def is_tmux_name(value: object) -> bool:
    return isinstance(value, str) and _TMUX_NAME.fullmatch(value) is not None


def presence_key(machine_pub: str, tmux_name: str) -> str:
    return f"{machine_pub}:{tmux_name}"


def split_key(key: str) -> tuple[str, str] | None:
    """``(machine_pub, tmux_name)`` of a well-formed key, else None."""
    machine_pub, sep, tmux_name = str(key).partition(":")
    if not sep or not _HEX64.fullmatch(machine_pub):
        return None
    if not is_tmux_name(tmux_name):
        return None
    return machine_pub, tmux_name


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="machine_pub:tmux_name")
class PersonalSessionPresenceV1(SettingSchema):
    set_id = PERSONAL_SESSION_PRESENCE_SET_ID
    schema_revision = PERSONAL_SESSION_PRESENCE_REVISION

    machine_id: str = field(
        required=True,
        description=(
            "Durable roster machine id of the machine the session runs on; "
            "readers resolve its display name through the machine profile set"
        ),
    )
    label: str = field(
        required=False, description="Session working title, if one is set",
    )
    project: str = field(required=False, description="Workspace id")
    type: str = field(
        required=False, description="Session type (container, host, ...)",
    )
    harness: str = field(required=False, description="Running CLI harness")
    model: str = field(required=False, description="Most recent model id")
    role: str = field(required=False, description="Session role, if set")
    state: str = field(
        required=True,
        description="Lifecycle state: LAUNCHING, ACTIVE or STOPPING",
    )
    since: int = field(
        required=True, description="Unix seconds the session was created",
    )
    launched_by: str = field(
        required=False,
        description="Who started it; set by a remote launch (auto-8xlgo)",
    )
    home_machine: str = field(
        required=False,
        description="machine_pub of the dashboard that launched it, if remote",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        if not _HEX64.fullmatch(str(payload.get("machine_id", ""))):
            raise SchemaValidationError(
                f"{cls.__name__}: 'machine_id' must be 64 lowercase hex"
            )
        if payload.get("state") not in STATES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'state' must be one of {list(STATES)}"
            )
        home_machine = payload.get("home_machine")
        if home_machine is not None and not _HEX64.fullmatch(str(home_machine)):
            raise SchemaValidationError(
                f"{cls.__name__}: 'home_machine' must be 64 lowercase hex"
            )
