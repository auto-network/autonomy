"""``autonomy.network.peer-connections#1`` -- how long this machine keeps an
idle peer connection open (graph://9642ab99-bae, connection lifecycle rule 5).

A peer connection is one authenticated session to one peer machine in one
scope. The CALLER closes a connection that has had no record sent or
received for ``idle_close_s`` seconds; the serving side never closes one for
idleness. Machine-homed: how many connections this machine holds open is a
fact about this computer.
"""

from __future__ import annotations

from .registry import (
    SchemaValidationError,
    SettingSchema,
    home,
    publication_band,
    singleton,
)

PEER_CONNECTIONS_SET_ID = "autonomy.network.peer-connections"
SCHEMA_REVISION = 1
DEFAULT_IDLE_CLOSE_S = 900
_IDLE_CLOSE_RANGE = (10, 7 * 24 * 3600)

SYNOPSIS = {
    "summary": "How long this machine keeps an idle peer connection open before closing it.",
    "nouns": ["peer connection", "idle close", "connection pool", "session control"],
    "related_set_ids": [],
}


@publication_band(min="raw", max="curated")
@home("machine")
@singleton(key="default")
class PeerConnectionsV1(SettingSchema):
    """Shape of an ``autonomy.network.peer-connections#1`` payload."""

    set_id = PEER_CONNECTIONS_SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "idle_close_s": {
            "type": "integer",
            "description": ("Seconds with no record sent or received after which "
                            "this machine closes a peer connection it opened."),
            "default": DEFAULT_IDLE_CLOSE_S,
        },
    }

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)
        value = payload.get("idle_close_s")
        if value is None:
            return
        low, high = _IDLE_CLOSE_RANGE
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise SchemaValidationError(
                f"{cls.__name__}: 'idle_close_s' must be an integer in {low}..{high}, "
                f"got {value!r}")
