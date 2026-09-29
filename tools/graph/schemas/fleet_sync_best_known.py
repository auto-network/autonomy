"""Machine-local, per scope: the best cursor any peer has reported per origin.

Homed in ``machine.db`` like ``autonomy.machine.fleet-sync-peer-scope``: a
sync completion must never author a mutation into the database it synchronized.

Every pull a peer makes carries its whole cursor map (``body["watermarks"]``).
The serve path folds it in on arrival: for each origin, the highest cursor any
peer has claimed, and which peer claimed it. This machine's own lag in the
scope is then ``max over origins o of (best[o] - our cursor for o)`` -- the
same origin on both sides, and O(origins) whatever the number of peers. Whole
per-peer maps are not stored: that is machines x origins per machine, O(N^2)
in an organization. Definitions: graph://6aa9bffc-ca9 Record 3;
graph://1155b8f4-8cf "Terms, and which frontier is which"; pitfall
graph://e6dba57c-f8b.
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

FLEET_SYNC_BEST_KNOWN_SET_ID = "autonomy.machine.fleet-sync-best-known"
FLEET_SYNC_BEST_KNOWN_REVISION = 1
#: The pull request's own bound (MAX_WATERMARK_ORIGINS).
MAX_BEST_KNOWN_ORIGINS = 4096

SYNOPSIS = {
    "summary": (
        "Machine-local, per scope: the best cursor any peer has reported for "
        "each origin, folded from the watermark maps peers send on their "
        "pulls. This machine's Fleet lag is derived from it per origin."
    ),
    "nouns": [
        "fleet sync best known position",
        "fleet sync lag",
        "fleet sync convergence",
    ],
    "related_set_ids": [
        "autonomy.machine.fleet-sync-peer-scope",
    ],
}

_HEX = frozenset("0123456789abcdef")


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="scope")
class FleetSyncBestKnownV1(SettingSchema):
    """One scope's best reported cursor per origin."""

    set_id = FLEET_SYNC_BEST_KNOWN_SET_ID
    schema_revision = FLEET_SYNC_BEST_KNOWN_REVISION

    origins: dict = field(
        required=True,
        description=(
            "{origin machine key: {ns: the highest cursor any peer has "
            "reported for that origin, peer: the machine key of the peer "
            "that reported it}}. Only ever raised, as cursors only advance."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        origins = payload.get("origins")
        if not isinstance(origins, dict) or len(origins) > MAX_BEST_KNOWN_ORIGINS:
            raise SchemaValidationError(
                f"origins must be an object of at most {MAX_BEST_KNOWN_ORIGINS} entries")
        for origin, best in origins.items():
            if not _hex64(origin):
                raise SchemaValidationError("origins keys must be 64 lowercase hex origin ids")
            if not isinstance(best, dict) or set(best) != {"ns", "peer"}:
                raise SchemaValidationError("each origin must carry exactly ns and peer")
            ns = best["ns"]
            if isinstance(ns, bool) or not isinstance(ns, int) or ns < 0:
                raise SchemaValidationError("ns must be a non-negative integer")
            if not _hex64(best["peer"]):
                raise SchemaValidationError("peer must be a 64 lowercase hex machine key")
