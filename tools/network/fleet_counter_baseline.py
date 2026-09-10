"""Record and apply the operator's Fleet counter reset.

Writes one row per peer and scope holding the counter values at the moment the
reset ran, and subtracts them at read time.  Nothing a connector writes is
touched, so a reset cannot lose an update to a serve that is in flight.
"""

from __future__ import annotations

import time

from tools.graph import settings_ops
from tools.graph.schemas.fleet_counter_baseline import (
    FLEET_COUNTER_BASELINE_REVISION,
    FLEET_COUNTER_BASELINE_SET_ID,
    FleetCounterBaselineV1,
)

#: The single row covering a peer's machine-wide counters, as distinct from
#: its per-scope ones. A scope slug cannot contain ':' so this cannot collide.
MACHINE_SCOPE = "*"


def baseline_key(peer: str, scope: str) -> str:
    if not isinstance(peer, str) or not peer:
        raise ValueError("peer must be a non-empty string")
    if not isinstance(scope, str) or not scope or ":" in scope:
        raise ValueError("scope must be a plain slug")
    return f"{peer}:{scope}"


def record(
    entries: dict[tuple[str, str], dict], *, at_ns: int | None = None,
    org: str = "machine",
) -> int:
    """Write ``{(peer, scope): counters}`` as the new baseline."""
    stamp = time.time_ns() if at_ns is None else int(at_ns)
    written = 0
    for (peer, scope), counters in entries.items():
        payload = {"reset_at_ns": stamp}
        for name in (
            "bytes_sent", "bytes_received", "transactions_applied",
            "attempts_failed",
        ):
            value = counters.get(name)
            if value:
                payload[name] = int(value)
        FleetCounterBaselineV1.validate(payload)
        settings_ops.upsert_by_key(
            FLEET_COUNTER_BASELINE_SET_ID,
            FLEET_COUNTER_BASELINE_REVISION,
            baseline_key(peer, scope),
            payload,
            org=org,
            state="raw",
        )
        written += 1
    return written


def read(*, org: str = "machine") -> dict[tuple[str, str], dict]:
    """``{(peer, scope): baseline}``, for the view to subtract."""
    out: dict[tuple[str, str], dict] = {}
    members = settings_ops.read_owned_set(
        FLEET_COUNTER_BASELINE_SET_ID,
        org=org,
        target_revision=FLEET_COUNTER_BASELINE_REVISION,
    ).members
    for member in members:
        peer, _, scope = str(member.key).partition(":")
        if not peer or not scope:
            continue
        out[(peer, scope)] = dict(member.payload or {})
    return out


def since(value: object, baseline: dict | None, name: str) -> int:
    """``value`` minus what was cleared, floored at zero.

    Floored because the counters are the only truth about what moved and a
    baseline can outlive them -- a telemetry row rebuilt from zero would
    otherwise render a negative total, which is a worse answer than nothing.
    """
    current = int(value or 0)
    if not baseline:
        return current
    return max(0, current - int(baseline.get(name) or 0))
