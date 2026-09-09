"""Machine-local Fleet transfer rate, as two fixed ring buffers.

Homed in ``machine.db`` for the same reason as
``autonomy.machine.fleet-sync-telemetry``: a sync completion must never author
a mutation into the personal database it just synchronized.

Peer is deliberately absent from the key. Rate is a property of a transport,
a byte direction and a scope; attributing it per peer as well would make the
row count grow with the fleet without answering a question the fleet view
asks. Per-peer totals live in ``autonomy.machine.fleet-sync-peer-scope``.

The two rings are what make this bounded. A slot is chosen by
``epoch_minute % 60`` / ``epoch_hour % 24`` and carries the epoch it was
written for. A writer zeroes a slot whose stamp is not the current epoch
before adding to it, and a reader counts a slot only when its stamp equals
the epoch it is asking for. Both halves enforce it, so a slot from 59 minutes
ago can never be read as current, and no pruning job ever has to run.
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


FLEET_SYNC_TRAFFIC_SET_ID = "autonomy.machine.fleet-sync-traffic"
FLEET_SYNC_TRAFFIC_REVISION = 1

#: Ring lengths. These are the modulus as well as the length: changing either
#: number silently re-points every existing slot, so a change is a new schema
#: revision with a fresh key space, never an in-place edit.
MINUTE_SLOTS = 60
HOUR_SLOTS = 24

TRANSPORTS = ("direct", "relay")
#: Byte direction, not sync role. One pull attempt both sends and receives, so
#: "pull"/"serve" cannot be recovered from a byte count and would force every
#: reader to guess which half of an attempt a number came from.
DIRECTIONS = ("sent", "received")

SYNOPSIS = {
    "summary": (
        "Machine-local Fleet transfer bytes per transport, byte direction and "
        "scope, as a 60-minute and a 24-hour ring. These observations never "
        "enter personal Fleet sync."
    ),
    "nouns": [
        "fleet sync traffic",
        "fleet sync rate",
        "fleet transfer history",
        "fleet bandwidth",
    ],
    "related_set_ids": [
        "autonomy.machine.fleet-sync-telemetry",
        "autonomy.machine.fleet-sync-peer-scope",
    ],
}


def _ring(payload: dict, name: str, length: int) -> None:
    value = payload.get(name)
    if not isinstance(value, list) or len(value) != length:
        raise SchemaValidationError(
            f"{name} must be a list of exactly {length} integers"
        )
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int) or entry < 0:
            raise SchemaValidationError(
                f"{name} entries must be non-negative integers"
            )


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="transport_direction_scope")
class FleetSyncTrafficV1(SettingSchema):
    """One transport/direction/scope rate history, in two fixed rings."""

    set_id = FLEET_SYNC_TRAFFIC_SET_ID
    schema_revision = FLEET_SYNC_TRAFFIC_REVISION

    minute_bytes: list = field(
        required=True,
        description=(
            f"Bytes observed in each of {MINUTE_SLOTS} minute slots, indexed "
            "by epoch minute modulo the ring length."
        ),
    )
    minute_stamp: list = field(
        required=True,
        description=(
            "Epoch minute each minute slot was written for. A slot whose "
            "stamp is not the epoch being asked about holds stale bytes and "
            "counts as zero."
        ),
    )
    hour_bytes: list = field(
        required=True,
        description=(
            f"Bytes observed in each of {HOUR_SLOTS} hour slots, indexed by "
            "epoch hour modulo the ring length."
        ),
    )
    hour_stamp: list = field(
        required=True,
        description="Epoch hour each hour slot was written for.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _ring(payload, "minute_bytes", MINUTE_SLOTS)
        _ring(payload, "minute_stamp", MINUTE_SLOTS)
        _ring(payload, "hour_bytes", HOUR_SLOTS)
        _ring(payload, "hour_stamp", HOUR_SLOTS)
