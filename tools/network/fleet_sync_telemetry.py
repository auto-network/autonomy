"""Persist Fleet performance observations without feeding Fleet sync.

Every row lives in the machine-local Settings store.  The personal database
and its authored journal are never opened by this module.
"""

from __future__ import annotations

import threading
import time
from typing import Mapping

from tools.graph import settings_ops
from tools.graph.schemas.fleet_sync_telemetry import (
    FLEET_SYNC_TELEMETRY_REVISION,
    FLEET_SYNC_TELEMETRY_SET_ID,
    FleetSyncTelemetryV1,
)


_CHANNELS = {"direct", "relay"}
_DIRECTIONS = {"pull", "serve"}
_OUTCOMES = {"success", "failed", "cancelled"}
_lock = threading.RLock()


def telemetry_key(peer_machine_public_key: str, channel: str, direction: str) -> str:
    if (
        not isinstance(peer_machine_public_key, str)
        or len(peer_machine_public_key) != 64
        or any(ch not in "0123456789abcdef" for ch in peer_machine_public_key)
    ):
        raise ValueError("peer machine public key must be 64 lowercase hex characters")
    if channel not in _CHANNELS:
        raise ValueError("Fleet telemetry channel must be direct or relay")
    if direction not in _DIRECTIONS:
        raise ValueError("Fleet telemetry direction must be pull or serve")
    return f"{channel}:{direction}:{peer_machine_public_key}"


def _zero_payload() -> dict:
    return {
        "iterations": 0,
        "successful_iterations": 0,
        "failed_iterations": 0,
        "cancelled_iterations": 0,
        "total_duration_ms": 0,
        "last_duration_ms": 0,
        "total_bytes_sent": 0,
        "total_bytes_received": 0,
        "last_bytes_sent": 0,
        "last_bytes_received": 0,
        "total_mutation_frames": 0,
        "last_mutation_frames": 0,
        "total_transactions": 0,
        "last_transactions": 0,
        "total_checkpoint_bytes": 0,
        "last_checkpoint_bytes": 0,
        "acknowledged_transaction_ref": 0,
        "last_mode": "delta",
        "last_outcome": "success",
        "last_error_code": "",
        "last_started_at_ns": 0,
        "last_finished_at_ns": 0,
        "last_success_at_ns": 0,
    }


def record_iteration(
    peer_machine_public_key: str,
    *,
    channel: str,
    direction: str,
    mode: str,
    outcome: str,
    started_at_ns: int,
    duration_ms: int,
    bytes_sent: int = 0,
    bytes_received: int = 0,
    mutation_frames: int = 0,
    transactions: int = 0,
    checkpoint_bytes: int = 0,
    error_code: str = "",
    acknowledged_transaction_ref: int | None = None,
    org: str = "machine",
) -> dict:
    """Atomically advance one local telemetry aggregate and return its payload."""
    key = telemetry_key(peer_machine_public_key, channel, direction)
    if mode not in {"delta", "checkpoint"}:
        raise ValueError("Fleet telemetry mode must be delta or checkpoint")
    if outcome not in _OUTCOMES:
        raise ValueError("Fleet telemetry outcome is invalid")
    values = {
        "duration_ms": duration_ms,
        "bytes_sent": bytes_sent,
        "bytes_received": bytes_received,
        "mutation_frames": mutation_frames,
        "transactions": transactions,
        "checkpoint_bytes": checkpoint_bytes,
        "started_at_ns": started_at_ns,
    }
    if acknowledged_transaction_ref is not None:
        values["acknowledged_transaction_ref"] = acknowledged_transaction_ref
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in values.values()):
        raise ValueError("Fleet telemetry measurements must be non-negative integers")

    # A process-local lock closes the read/modify/write race between the direct
    # and relay tasks in one Dashboard.  Keys include transport + direction,
    # so the serving connector and pulling Dashboard never contend cross-process.
    with _lock:
        row = settings_ops.read_set_key(
            FLEET_SYNC_TELEMETRY_SET_ID, key, org=org, peers=[]
        )
        payload = _zero_payload()
        if row is not None:
            payload.update(row["payload"])
        payload.update({
            "iterations": payload["iterations"] + 1,
            "successful_iterations": (
                payload["successful_iterations"] + int(outcome == "success")
            ),
            "failed_iterations": (
                payload["failed_iterations"] + int(outcome == "failed")
            ),
            "cancelled_iterations": (
                payload["cancelled_iterations"] + int(outcome == "cancelled")
            ),
            "total_duration_ms": payload["total_duration_ms"] + duration_ms,
            "last_duration_ms": duration_ms,
            "total_bytes_sent": payload["total_bytes_sent"] + bytes_sent,
            "total_bytes_received": payload["total_bytes_received"] + bytes_received,
            "last_bytes_sent": bytes_sent,
            "last_bytes_received": bytes_received,
            "total_mutation_frames": payload["total_mutation_frames"] + mutation_frames,
            "last_mutation_frames": mutation_frames,
            "total_transactions": payload["total_transactions"] + transactions,
            "last_transactions": transactions,
            "total_checkpoint_bytes": payload["total_checkpoint_bytes"] + checkpoint_bytes,
            "last_checkpoint_bytes": checkpoint_bytes,
            "last_mode": mode,
            "last_outcome": outcome,
            "last_error_code": str(error_code)[:160] if outcome != "success" else "",
            "last_started_at_ns": started_at_ns,
            "last_finished_at_ns": time.time_ns(),
        })
        if outcome == "success":
            payload["last_success_at_ns"] = payload["last_finished_at_ns"]
        if acknowledged_transaction_ref is not None and outcome == "success":
            payload["acknowledged_transaction_ref"] = max(
                int(payload["acknowledged_transaction_ref"]),
                acknowledged_transaction_ref,
            )
        FleetSyncTelemetryV1.validate(payload)
        settings_ops.upsert_by_key(
            FLEET_SYNC_TELEMETRY_SET_ID,
            FLEET_SYNC_TELEMETRY_REVISION,
            key,
            payload,
            org=org,
            state="raw",
        )
        return payload


def read_peer_totals(*, org: str = "machine") -> dict[str, dict]:
    """Aggregate all local channel/direction observations by remote peer."""
    result: dict[str, dict] = {}
    members = settings_ops.read_owned_set(
        FLEET_SYNC_TELEMETRY_SET_ID,
        org=org,
        target_revision=FLEET_SYNC_TELEMETRY_REVISION,
    ).members
    for member in members:
        parts = member.key.split(":", 2)
        if len(parts) != 3:
            continue
        channel, direction, peer = parts
        try:
            telemetry_key(peer, channel, direction)
        except ValueError:
            continue
        payload: Mapping = member.payload
        current = result.setdefault(peer, {
            "iterations": 0,
            "successful_iterations": 0,
            "failed_iterations": 0,
            "cancelled_iterations": 0,
            "total_duration_ms": 0,
            "bytes_sent": 0,
            "bytes_received": 0,
            "mutation_frames": 0,
            "transactions": 0,
            "checkpoint_bytes": 0,
            "last_duration_ms": 0,
            "last_finished_at_ns": 0,
            "last_success_at_ns": 0,
            "last_outcome": None,
            "last_error_code": None,
        })
        for target, source in (
            ("iterations", "iterations"),
            ("successful_iterations", "successful_iterations"),
            ("failed_iterations", "failed_iterations"),
            ("cancelled_iterations", "cancelled_iterations"),
            ("total_duration_ms", "total_duration_ms"),
            ("bytes_sent", "total_bytes_sent"),
            ("bytes_received", "total_bytes_received"),
            ("mutation_frames", "total_mutation_frames"),
            ("transactions", "total_transactions"),
            ("checkpoint_bytes", "total_checkpoint_bytes"),
        ):
            current[target] += int(payload.get(source) or 0)
        finished = int(payload.get("last_finished_at_ns") or 0)
        current["last_success_at_ns"] = max(
            current["last_success_at_ns"],
            int(payload.get("last_success_at_ns") or 0),
        )
        if finished >= current["last_finished_at_ns"]:
            current.update({
                "last_duration_ms": int(payload.get("last_duration_ms") or 0),
                "last_finished_at_ns": finished,
                "last_outcome": payload.get("last_outcome"),
                "last_error_code": payload.get("last_error_code") or None,
            })
    return result


def read_acknowledged_transaction_ref(
    peer_machine_public_key: str,
    *,
    org: str = "machine",
) -> int:
    """Return the greatest locally acknowledged position for one source peer."""
    acknowledged = 0
    for channel in _CHANNELS:
        key = telemetry_key(peer_machine_public_key, channel, "pull")
        row = settings_ops.read_set_key(
            FLEET_SYNC_TELEMETRY_SET_ID, key, org=org, peers=[]
        )
        if row is None:
            continue
        acknowledged = max(
            acknowledged,
            int(row["payload"].get("acknowledged_transaction_ref") or 0),
        )
    return acknowledged
