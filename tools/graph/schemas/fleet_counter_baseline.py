"""What the operator's counter reset cleared, and when.

A reset that MUTATES counters cannot be made correct here. The dashboard
performs it while four connector processes are read-modify-writing the same
telemetry rows on every serve; both sides take a ``threading`` lock, which is
process-local and buys nothing across processes. Measured 2026-09-10: one reset
cleared 3,348 failed attempts and 1.27 GB received, moved 52.7 GB sent only as
far as 28.0 GB, and left transactions applied untouched. A lost update, on the
counters written most often.

The fix is not a cross-process lock. Putting a settings-store lock in the path
of every sync completion would make the bookkeeping contend with the work it
describes, which is the shape of defect this record exists to end.

So the reset stops mutating anything. It writes ONE row -- the counter values at
the moment it ran -- and the view subtracts. The connectors keep writing
monotonically and never contend. The subtraction is exact however much traffic
is in flight, the real lifetime totals survive on disk, and "since you reset"
is what an operator means by resetting a counter anyway.
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


FLEET_COUNTER_BASELINE_SET_ID = "autonomy.machine.fleet-counter-baseline"
FLEET_COUNTER_BASELINE_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Machine-local record of what a Fleet counter reset cleared, so the "
        "view can subtract it without mutating counters other processes are "
        "writing."
    ),
    "nouns": [
        "fleet counter reset",
        "fleet counter baseline",
        "fleet statistics reset",
    ],
    "related_set_ids": [
        "autonomy.machine.fleet-sync-telemetry",
        "autonomy.machine.fleet-sync-peer-scope",
    ],
}


def _counter(payload: dict, name: str) -> None:
    value = payload.get(name)
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaValidationError(f"{name} must be a non-negative integer")


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="peer_scope")
class FleetCounterBaselineV1(SettingSchema):
    """One peer/scope's counter values at the moment of the last reset.

    Keyed the same way the counters are, so a reset is recorded at the same
    grain it is displayed at and no aggregation has to guess.
    """

    set_id = FLEET_COUNTER_BASELINE_SET_ID
    schema_revision = FLEET_COUNTER_BASELINE_REVISION

    reset_at_ns: int = field(
        required=True,
        description="Wall-clock nanoseconds when the operator reset counters.",
    )
    bytes_sent: int = field(
        required=False, description="Cumulative bytes sent at the reset."
    )
    bytes_received: int = field(
        required=False, description="Cumulative bytes received at the reset."
    )
    transactions_applied: int = field(
        required=False, description="Transactions applied at the reset."
    )
    attempts_failed: int = field(
        required=False, description="Failed attempts at the reset."
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        for name in (
            "reset_at_ns", "bytes_sent", "bytes_received",
            "transactions_applied", "attempts_failed",
        ):
            _counter(payload, name)
