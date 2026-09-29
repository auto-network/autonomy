"""Machine-local per-peer, per-scope Fleet sync position and totals.

Homed in ``machine.db`` for the same reason as
``autonomy.machine.fleet-sync-telemetry``: a sync completion must never author
a mutation into the personal database it just synchronized.

``frontier_ns`` is not an observation this module invents. It is the peer's
own **watermark promise** (design of record ``graph://1155b8f4-8cf``): for
origin ``p``, every ``p``-authored mutation at or below it is committed and
available, and ``p`` will permanently refuse any later write at or below it.
The peer already publishes that map in ``body["watermarks"]`` on every pull
request, because the server cannot compute a delta without it. This record
persists what already arrives; it adds no wire field and no new exchange.

The map is reduced ON ARRIVAL, never stored whole (that would be O(N^2) per
machine in an organization): ``behind_ns`` / ``behind_origin`` /
``unreceived`` are how far the peer trails THIS machine, computed once per
pull against this machine's cursors, per origin -- max over origins o of
``W_L[o] - W_P[o]``. How far this machine trails its peers comes from the
per-scope best-known vector (autonomy.machine.fleet-sync-best-known). Lag is
NOT ``now - frontier_ns``, and ``frontier_ns`` (a minimum across the peer's
origins) is a summary kept for diagnostics only. Definitions:
graph://6aa9bffc-ca9 Record 3; graph://1155b8f4-8cf "Terms, and which frontier
is which"; pitfall graph://e6dba57c-f8b.
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


FLEET_SYNC_PEER_SCOPE_SET_ID = "autonomy.machine.fleet-sync-peer-scope"
FLEET_SYNC_PEER_SCOPE_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Machine-local per-peer, per-scope Fleet sync frontier and transfer "
        "totals. These observations never enter personal Fleet sync."
    ),
    "nouns": [
        "fleet sync frontier",
        "fleet sync lag",
        "fleet sync convergence",
        "fleet peer scope totals",
    ],
    "related_set_ids": [
        "autonomy.machine.fleet-sync-telemetry",
        "autonomy.machine.fleet-sync-traffic",
    ],
}


def _counter(payload: dict, name: str) -> None:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaValidationError(f"{name} must be a non-negative integer")


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="peer_scope")
class FleetSyncPeerScopeV1(SettingSchema):
    """One peer's position and running totals in one scope."""

    set_id = FLEET_SYNC_PEER_SCOPE_SET_ID
    schema_revision = FLEET_SYNC_PEER_SCOPE_REVISION

    frontier_ns: int = field(
        required=True,
        description=(
            "Lowest timestamp across the origins the peer promises for "
            "this scope: a summary of the peer's slowest origin, kept for "
            "diagnostics. No lag is computed from it (see behind_ns)."
        ),
    )
    behind_ns: int = field(
        required=False,
        description=(
            "How far the peer trailed THIS machine at its last pull: max "
            "over origins of (our cursor - the peer's cursor), same origin "
            "on both sides. 0 when it held everything we held."
        ),
    )
    behind_origin: str = field(
        required=False,
        description="The origin the peer trailed most on, or empty.",
    )
    unreceived: int = field(
        required=False,
        description=(
            "How many origins we hold that the peer held nothing of at its "
            "last pull. Any makes its lag unknown ('not yet received')."
        ),
    )
    observed_at_ns: int = field(
        required=True,
        description=(
            "Wall-clock nanoseconds when that promise was last received. It "
            "is stamped when the peer's PULL REQUEST is decoded, so it means "
            "last heard from, NOT last successfully synced with: a peer that "
            "asks and then fails still updates it."
        ),
    )
    bytes_in: int = field(
        required=True,
        description=(
            "Cumulative application bytes received from this peer for this "
            "scope. Reset to zero by the operator's counter reset."
        ),
    )
    bytes_out: int = field(
        required=True,
        description=(
            "Cumulative application bytes sent to this peer for this scope. "
            "Reset to zero by the operator's counter reset."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        for name in ("frontier_ns", "observed_at_ns", "bytes_in", "bytes_out"):
            _counter(payload, name)
        for name in ("behind_ns", "unreceived"):
            if name in payload:
                _counter(payload, name)
        if "behind_origin" in payload and not isinstance(payload["behind_origin"], str):
            raise SchemaValidationError("behind_origin must be a string")
