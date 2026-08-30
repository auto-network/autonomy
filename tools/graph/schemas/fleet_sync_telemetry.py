"""Machine-local performance telemetry for Fleet synchronization.

The payload is deliberately homed in ``machine.db``.  A sync completion must
never author a mutation into the personal database it just synchronized: that
would turn observation into more replicated work and create a feedback loop.
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


FLEET_SYNC_TELEMETRY_SET_ID = "autonomy.machine.fleet-sync-telemetry"
FLEET_SYNC_TELEMETRY_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Machine-local cumulative Fleet synchronization timings and transfer "
        "counters. These observations never enter personal Fleet sync."
    ),
    "nouns": [
        "fleet sync telemetry",
        "fleet sync performance",
        "fleet transfer counters",
        "fleet sync duration",
    ],
    "related_set_ids": ["autonomy.machine.fleet-route"],
}

_OUTCOMES = {"success", "failed", "cancelled"}
_MODES = {"delta", "checkpoint"}


def _counter(payload: dict, name: str) -> None:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaValidationError(f"{name} must be a non-negative integer")


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="peer_transport_direction")
class FleetSyncTelemetryV1(SettingSchema):
    """One cumulative observation stream for a peer/transport/direction key."""

    set_id = FLEET_SYNC_TELEMETRY_SET_ID
    schema_revision = FLEET_SYNC_TELEMETRY_REVISION

    iterations: int = field(required=True, description="Terminal sync attempts observed.")
    successful_iterations: int = field(required=True, description="Attempts that completed and verified.")
    failed_iterations: int = field(required=True, description="Attempts that ended in failure.")
    cancelled_iterations: int = field(required=True, description="Attempts cancelled during shutdown or reconfiguration.")
    total_duration_ms: int = field(required=True, description="Cumulative wall duration of all terminal attempts.")
    last_duration_ms: int = field(required=True, description="Wall duration of the newest terminal attempt.")
    total_bytes_sent: int = field(required=True, description="Cumulative application bytes sent.")
    total_bytes_received: int = field(required=True, description="Cumulative application bytes received.")
    last_bytes_sent: int = field(required=True, description="Application bytes sent by the newest attempt.")
    last_bytes_received: int = field(required=True, description="Application bytes received by the newest attempt.")
    total_mutation_frames: int = field(required=True, description="Cumulative mutation frames transferred.")
    last_mutation_frames: int = field(required=True, description="Mutation frames transferred by the newest attempt.")
    total_transactions: int = field(required=True, description="Cumulative authored transactions transferred.")
    last_transactions: int = field(required=True, description="Authored transactions transferred by the newest attempt.")
    total_checkpoint_bytes: int = field(required=True, description="Cumulative checkpoint file payload bytes transferred.")
    last_checkpoint_bytes: int = field(required=True, description="Checkpoint file payload bytes in the newest attempt.")
    acknowledged_transaction_ref: int = field(required=True, description="Newest fully verified source-journal transaction position.")
    resume_breadcrumbs: list = field(required=False, description="Resume breadcrumbs, each naming one verified transaction (origin, transaction id, timestamp), newest first: the recent few contiguously plus an exponentially thinned history. Presented on pull so a restored source recomputes the resume position from content, never from its renumbered row ids.")
    resume_breadcrumb_seq: int = field(required=False, description="Monotonic acknowledgement counter that ages the breadcrumb trail.")
    last_mode: str = field(required=True, description="Newest attempt mode: delta or checkpoint.")
    last_outcome: str = field(required=True, description="Newest terminal outcome.")
    last_error_code: str = field(required=True, description="Bounded failure classification, empty after success.")
    last_started_at_ns: int = field(required=True, description="Wall-clock nanoseconds when the newest attempt started.")
    last_finished_at_ns: int = field(required=True, description="Wall-clock nanoseconds when the newest attempt finished.")
    last_success_at_ns: int = field(required=True, description="Wall-clock nanoseconds of the newest successful attempt.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        for name in (
            "iterations", "successful_iterations", "failed_iterations",
            "cancelled_iterations", "total_duration_ms", "last_duration_ms",
            "total_bytes_sent", "total_bytes_received", "last_bytes_sent",
            "last_bytes_received", "total_mutation_frames",
            "last_mutation_frames", "total_transactions", "last_transactions",
            "total_checkpoint_bytes", "last_checkpoint_bytes",
            "acknowledged_transaction_ref",
            "last_started_at_ns", "last_finished_at_ns", "last_success_at_ns",
        ):
            _counter(payload, name)
        breadcrumbs = payload.get("resume_breadcrumbs")
        if breadcrumbs is not None:
            if not isinstance(breadcrumbs, list) or len(breadcrumbs) > 64:
                raise SchemaValidationError(
                    "resume_breadcrumbs must be a list of at most 64 entries"
                )
            for entry in breadcrumbs:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("origin"), str)
                    or len(entry["origin"]) != 64
                    or not isinstance(entry.get("transaction"), str)
                    or not entry["transaction"]
                    or isinstance(entry.get("timestamp"), bool)
                    or not isinstance(entry.get("timestamp"), int)
                    or entry["timestamp"] < 0
                    or isinstance(entry.get("seq"), bool)
                    or not isinstance(entry.get("seq"), int)
                    or entry["seq"] < 0
                ):
                    raise SchemaValidationError(
                        "resume_breadcrumbs entries must carry origin, "
                        "transaction, timestamp, and seq"
                    )
        seq = payload.get("resume_breadcrumb_seq")
        if seq is not None and (
            isinstance(seq, bool) or not isinstance(seq, int) or seq < 0
        ):
            raise SchemaValidationError(
                "resume_breadcrumb_seq must be a non-negative integer"
            )
        if payload.get("last_mode") not in _MODES:
            raise SchemaValidationError("last_mode must be delta or checkpoint")
        if payload.get("last_outcome") not in _OUTCOMES:
            raise SchemaValidationError("last_outcome is invalid")
        error = payload.get("last_error_code")
        if not isinstance(error, str) or len(error) > 160:
            raise SchemaValidationError("last_error_code must be bounded text")
