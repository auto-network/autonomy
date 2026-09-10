"""The monotonic generation a machine stamps on its reachability descriptor.

Machine-homed on purpose, and the reason is the failure it prevents. A reader
fences descriptors by ordering: newer wins. If the counter ever restarts, a
machine republishes generation 1 while a peer holds 7, the peer treats the
CURRENT descriptor as stale, and it routes on addresses that may be gone --
indefinitely, with nothing logged at either end. Silent and permanent, which is
the shape of every defect found on 2026-09-09.

The counter therefore cannot live where the value is published. The published
row may be unreachable at exactly the moment a republish is needed, which is
during the outage the republish exists to recover from. It lives here instead:
durable across restarts, no network in its read path, and unaffected by the
failure that is blocking the publish.

That is not the two-copies defect. The local row is the SOURCE and the
descriptor carries a PUBLISHED COPY; there is no case where they disagree and
something has to choose between them.
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


FLEET_DESCRIPTOR_GENERATION_SET_ID = "autonomy.machine.fleet-descriptor-generation"
FLEET_DESCRIPTOR_GENERATION_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Machine-local monotonic counter stamped on this machine's published "
        "reachability descriptor, so a peer can order two descriptors without "
        "reading a clock."
    ),
    "nouns": [
        "fleet descriptor generation",
        "reachability descriptor ordering",
        "fleet descriptor fencing",
    ],
    "related_set_ids": ["autonomy.machine.fleet-direct"],
}


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="singleton")
class FleetDescriptorGenerationV1(SettingSchema):
    """This machine's next descriptor generation."""

    set_id = FLEET_DESCRIPTOR_GENERATION_SET_ID
    schema_revision = FLEET_DESCRIPTOR_GENERATION_REVISION

    generation: int = field(
        required=True,
        description=(
            "Highest generation this machine has ever MINTED, whether or not "
            "publishing it succeeded. Minting consumes the number, so a failed "
            "publish leaves a gap and never a repeat."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        value = payload.get("generation")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SchemaValidationError("generation must be a positive integer")
