"""Persist each peer's advertised frontier, and its per-scope byte totals.

Every row lives in the machine-local Settings store.  The personal database
and its authored journal are never opened by this module.

The frontier written here is not measured, it is *received*: the peer
publishes its per-origin watermark map in ``body["watermarks"]`` on every pull
request because the server cannot compute a delta without it (design of record
``graph://1155b8f4-8cf``).  Today the serve path uses that map to position the
pager and then discards it.  This module keeps it, so the Fleet view can say
how far behind a peer actually is rather than only when it last connected.
"""

from __future__ import annotations

import threading
import time
from typing import Mapping

from tools.graph import settings_ops
from tools.graph.schemas.fleet_sync_peer_scope import (
    FLEET_SYNC_PEER_SCOPE_REVISION,
    FLEET_SYNC_PEER_SCOPE_SET_ID,
    FleetSyncPeerScopeV1,
)

_lock = threading.RLock()


def peer_scope_key(peer_machine_public_key: str, scope: str) -> str:
    if (
        not isinstance(peer_machine_public_key, str)
        or len(peer_machine_public_key) != 64
        or any(ch not in "0123456789abcdef" for ch in peer_machine_public_key)
    ):
        raise ValueError("peer machine public key must be 64 lowercase hex characters")
    if not isinstance(scope, str) or not scope or ":" in scope:
        raise ValueError("Fleet peer scope must be a plain slug")
    return f"{peer_machine_public_key}:{scope}"


def _zero_payload() -> dict:
    return {
        "frontier_ns": 0,
        "observed_at_ns": 0,
        "bytes_in": 0,
        "bytes_out": 0,
    }


def _read(key: str, org: str) -> dict:
    row = settings_ops.read_set_key(
        FLEET_SYNC_PEER_SCOPE_SET_ID, key, org=org, peers=[]
    )
    payload = _zero_payload()
    if row is not None:
        for name in payload:
            value = (row["payload"] or {}).get(name)
            if not isinstance(value, bool) and isinstance(value, int) and value >= 0:
                payload[name] = value
    return payload


def _write(key: str, payload: dict, org: str) -> dict:
    FleetSyncPeerScopeV1.validate(payload)
    settings_ops.upsert_by_key(
        FLEET_SYNC_PEER_SCOPE_SET_ID,
        FLEET_SYNC_PEER_SCOPE_REVISION,
        key,
        payload,
        org=org,
        state="raw",
    )
    return payload


def record_frontier(
    peer_machine_public_key: str,
    *,
    scope: str,
    watermarks: Mapping[str, int],
    at_ns: int | None = None,
    org: str = "machine",
) -> dict:
    """Persist the frontier a peer just advertised for one scope.

    ``watermarks`` is the peer's whole per-origin map. What the Fleet view
    needs from it is one number — how current this peer is overall — which is
    the OLDEST origin it holds, not the newest: a peer that is up to date on
    four origins and sixteen hours behind on the fifth is sixteen hours
    behind, and taking the maximum would report it as current.

    An empty map is not a zero frontier. A store part-way through a bootstrap
    deliberately advertises nothing (``advertisable_origin_watermarks``), and
    recording that as "holds nothing from the beginning of time" would render
    a healthy joiner as infinitely behind. Such a request leaves the stored
    frontier untouched.
    """
    key = peer_scope_key(peer_machine_public_key, scope)
    values = [
        int(value) for value in (watermarks or {}).values()
        if not isinstance(value, bool) and isinstance(value, int) and value > 0
    ]
    if not values:
        return {}
    with _lock:
        payload = _read(key, org)
        payload["frontier_ns"] = min(values)
        payload["observed_at_ns"] = (
            time.time_ns() if at_ns is None else int(at_ns)
        )
        return _write(key, payload, org)


def record_bytes(
    peer_machine_public_key: str,
    *,
    scope: str,
    bytes_in: int = 0,
    bytes_out: int = 0,
    org: str = "machine",
) -> dict:
    """Add this attempt's bytes to one peer's per-scope totals."""
    key = peer_scope_key(peer_machine_public_key, scope)
    for value in (bytes_in, bytes_out):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Fleet peer scope bytes must be non-negative integers")
    if not bytes_in and not bytes_out:
        return {}
    with _lock:
        payload = _read(key, org)
        payload["bytes_in"] += bytes_in
        payload["bytes_out"] += bytes_out
        return _write(key, payload, org)


def read_peer_scopes(*, org: str = "machine") -> dict[str, list[dict]]:
    """``{peer: [{scope, frontier_ns, observed_at_ns, bytes_in, bytes_out}]}``.

    Lag is deliberately not computed here: it is ``now - frontier_ns`` at the
    moment of rendering, and a value aged inside a reader would be wrong by
    however long the response sat in a queue.
    """
    result: dict[str, list[dict]] = {}
    members = settings_ops.read_owned_set(
        FLEET_SYNC_PEER_SCOPE_SET_ID,
        org=org,
        target_revision=FLEET_SYNC_PEER_SCOPE_REVISION,
    ).members
    for member in members:
        parts = member.key.split(":")
        if len(parts) != 2:
            continue
        peer, scope = parts
        try:
            peer_scope_key(peer, scope)
        except ValueError:
            continue
        payload = member.payload or {}
        result.setdefault(peer, []).append({
            "scope": scope,
            "frontier_ns": int(payload.get("frontier_ns") or 0),
            "observed_at_ns": int(payload.get("observed_at_ns") or 0),
            "bytes_in": int(payload.get("bytes_in") or 0),
            "bytes_out": int(payload.get("bytes_out") or 0),
        })
    for scopes in result.values():
        scopes.sort(key=lambda row: row["scope"])
    return result


def reset_byte_totals(*, org: str = "machine") -> int:
    """Zero ``bytes_in``/``bytes_out`` on every row; return the rows changed.

    Only the two counters move. ``frontier_ns`` and ``observed_at_ns`` are the
    peer's own promise and when it was heard — resetting those would make
    every peer read as maximally behind until its next pull, which is a lie
    about convergence, not a cleared counter.
    """
    changed = 0
    with _lock:
        members = settings_ops.read_owned_set(
            FLEET_SYNC_PEER_SCOPE_SET_ID,
            org=org,
            target_revision=FLEET_SYNC_PEER_SCOPE_REVISION,
        ).members
        for member in members:
            payload = member.payload or {}
            if not payload.get("bytes_in") and not payload.get("bytes_out"):
                continue
            updated = dict(_zero_payload())
            updated["frontier_ns"] = int(payload.get("frontier_ns") or 0)
            updated["observed_at_ns"] = int(payload.get("observed_at_ns") or 0)
            _write(member.key, updated, org)
            changed += 1
    return changed
