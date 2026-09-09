"""Record Fleet transfer bytes into bounded rate rings.

Every row lives in the machine-local Settings store.  The personal database
and its authored journal are never opened by this module.

The whole point of this record is that it answers "how much moved in the last
hour" without ever growing.  Two fixed rings do that: a slot is addressed by
``epoch % ring length`` and carries the epoch it was written for, so a slot
that was last written 59 minutes ago is recognised as stale rather than read
as current.  There is no pruning path because there is nothing to prune.
"""

from __future__ import annotations

import threading
import time

from tools.graph import settings_ops
from tools.graph.schemas.fleet_sync_traffic import (
    DIRECTIONS,
    FLEET_SYNC_TRAFFIC_REVISION,
    FLEET_SYNC_TRAFFIC_SET_ID,
    HOUR_SLOTS,
    MINUTE_SLOTS,
    TRANSPORTS,
    FleetSyncTrafficV1,
)

MINUTE_SECONDS = 60
HOUR_SECONDS = 3600

_lock = threading.RLock()


def traffic_key(transport: str, direction: str, scope: str) -> str:
    if transport not in TRANSPORTS:
        raise ValueError(
            "Fleet traffic transport must be one of " + ", ".join(TRANSPORTS)
        )
    if direction not in DIRECTIONS:
        raise ValueError("Fleet traffic direction must be sent or received")
    if not isinstance(scope, str) or not scope or ":" in scope:
        raise ValueError("Fleet traffic scope must be a plain slug")
    return f"{transport}:{direction}:{scope}"


def _zero_payload() -> dict:
    return {
        "minute_bytes": [0] * MINUTE_SLOTS,
        "minute_stamp": [0] * MINUTE_SLOTS,
        "hour_bytes": [0] * HOUR_SLOTS,
        "hour_stamp": [0] * HOUR_SLOTS,
    }


def _add(payload: dict, bytes_key: str, stamp_key: str,
         slots: int, epoch: int, amount: int) -> None:
    """Add into the slot for ``epoch``, zeroing it first when it belongs to an
    older epoch.

    Without the stamp check a slot would accumulate this minute's bytes on top
    of the same minute one ring ago, and the ring would report a total that
    was never observed in any window.
    """
    slot = epoch % slots
    if payload[stamp_key][slot] != epoch:
        payload[stamp_key][slot] = epoch
        payload[bytes_key][slot] = 0
    payload[bytes_key][slot] += amount


def record_bytes(
    *,
    transport: str,
    direction: str,
    scope: str,
    amount: int,
    at_ns: int | None = None,
    org: str = "machine",
) -> dict:
    """Add ``amount`` bytes to this transport/direction/scope's rings."""
    key = traffic_key(transport, direction, scope)
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("Fleet traffic byte count must be a non-negative integer")
    if amount == 0:
        # Nothing moved: writing would only churn the row and re-stamp a slot
        # that has no bytes to report.
        return {}
    now_ns = time.time_ns() if at_ns is None else int(at_ns)
    seconds = now_ns // 1_000_000_000

    # A process-local lock closes the read/modify/write race between the direct
    # and relay tasks in one Dashboard.  Keys include transport, so the serving
    # connector and the pulling Dashboard never contend cross-process.
    with _lock:
        row = settings_ops.read_set_key(
            FLEET_SYNC_TRAFFIC_SET_ID, key, org=org, peers=[]
        )
        payload = _zero_payload()
        if row is not None:
            stored = row["payload"]
            for name, slots in (
                ("minute_bytes", MINUTE_SLOTS), ("minute_stamp", MINUTE_SLOTS),
                ("hour_bytes", HOUR_SLOTS), ("hour_stamp", HOUR_SLOTS),
            ):
                value = stored.get(name)
                # A ring of the wrong length cannot be re-indexed into the
                # current one — its slots mean different epochs.  Start clean
                # rather than report bytes against the wrong minute.
                if isinstance(value, list) and len(value) == slots:
                    payload[name] = [int(entry) for entry in value]
        _add(payload, "minute_bytes", "minute_stamp", MINUTE_SLOTS,
             seconds // MINUTE_SECONDS, amount)
        _add(payload, "hour_bytes", "hour_stamp", HOUR_SLOTS,
             seconds // HOUR_SECONDS, amount)
        FleetSyncTrafficV1.validate(payload)
        settings_ops.upsert_by_key(
            FLEET_SYNC_TRAFFIC_SET_ID,
            FLEET_SYNC_TRAFFIC_REVISION,
            key,
            payload,
            org=org,
            state="raw",
        )
        return payload


def read_traffic_rows(*, org: str = "machine") -> list[dict]:
    """Every rate row, shaped for the Fleet view.

    The stamps travel with the bytes: the reader decides which slots are
    current for the window it is drawing, so a row written by an older build
    cannot present stale slots as live.
    """
    rows: list[dict] = []
    members = settings_ops.read_owned_set(
        FLEET_SYNC_TRAFFIC_SET_ID,
        org=org,
        target_revision=FLEET_SYNC_TRAFFIC_REVISION,
    ).members
    for member in members:
        parts = member.key.split(":")
        if len(parts) != 3:
            continue
        transport, direction, scope = parts
        try:
            traffic_key(transport, direction, scope)
        except ValueError:
            continue
        payload = member.payload or {}
        rows.append({
            "transport": transport,
            "direction": direction,
            "scope": scope,
            "minute_bytes": list(payload.get("minute_bytes") or []),
            "minute_stamp": list(payload.get("minute_stamp") or []),
            "hour_bytes": list(payload.get("hour_bytes") or []),
            "hour_stamp": list(payload.get("hour_stamp") or []),
        })
    rows.sort(key=lambda row: (row["scope"], row["transport"], row["direction"]))
    return rows
