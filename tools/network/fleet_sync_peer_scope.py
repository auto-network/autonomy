"""Persist each peer's advertised frontier, and its per-scope byte totals.

Every row lives in the machine-local Settings store.  The personal database
and its authored journal are never opened by this module.

The frontier written here is not measured, it is *received*: the peer
publishes its per-origin watermark map in ``body["watermarks"]`` on every pull
request because the server cannot compute a delta without it (design of record
``graph://1155b8f4-8cf``).  The serve path uses that map to position the
pager; this module reduces it ON ARRIVAL, origin by origin: into how far the
peer trails this machine, and into one per-scope best-known cursor per origin
from which this machine's own lag is computed. O(origins) per pull and per
machine; whole maps are not stored (graph://6aa9bffc-ca9 Record 3; pitfall
graph://e6dba57c-f8b).
"""

from __future__ import annotations

import threading
import time
from typing import Mapping

from tools.graph import settings_ops
from tools.graph.schemas.fleet_sync_best_known import (
    FLEET_SYNC_BEST_KNOWN_REVISION,
    FLEET_SYNC_BEST_KNOWN_SET_ID,
    FleetSyncBestKnownV1,
)
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


_HEX = frozenset("0123456789abcdef")


def _clean_map(value) -> dict:
    """{origin: cursor} with only well-formed entries, at most 4096."""
    out: dict = {}
    if not isinstance(value, dict):
        return out
    for origin, cursor in value.items():
        if (isinstance(origin, str) and len(origin) == 64 and set(origin) <= _HEX
                and isinstance(cursor, int) and not isinstance(cursor, bool)
                and cursor > 0):
            out[origin] = cursor
            if len(out) >= 4096:
                break
    return out


#: The peer's lag against this machine, reduced from its map on arrival.
_REDUCED = ("behind_ns", "behind_origin", "unreceived")


def _trail(ahead: Mapping[str, int], held: Mapping[str, int]) -> dict:
    """How far ``held`` trails ``ahead``, per origin, reduced: the worst gap
    and its origin, and how many of ``ahead``'s origins ``held`` has nothing
    of. Same origin on both sides (graph://6aa9bffc-ca9 Record 3)."""
    behind_ns, behind_origin, unreceived = 0, "", 0
    for origin, position in (ahead or {}).items():
        mine = int((held or {}).get(origin) or 0)
        if position <= mine:
            continue
        if not mine:
            unreceived += 1
        elif position - mine > behind_ns:
            behind_ns, behind_origin = position - mine, origin
    return {"behind_ns": behind_ns, "behind_origin": behind_origin,
            "unreceived": unreceived}


def _fold_best_known(scope: str, peer: str, theirs: Mapping[str, int], org: str) -> None:
    """Fold the peer's map into this scope's best-known cursor per origin.

    Raised when any peer reports more. An entry the SAME peer vouched for
    follows that peer down: a lower report replaces it, and an origin the
    peer no longer reports is dropped -- a re-bootstrapped or rebuilt store
    must not leave a position nobody holds, which would read as a permanent
    false lag (review of eab8ba66). Other peers' next reports raise it again
    if they hold more. Written only when something changed."""
    row = settings_ops.read_set_key(FLEET_SYNC_BEST_KNOWN_SET_ID, scope, org=org, peers=[])
    origins = dict(((row or {}).get("payload") or {}).get("origins") or {})
    changed = False
    for origin, best in list(origins.items()):
        if isinstance(best, dict) and best.get("peer") == peer:
            position = theirs.get(origin)
            if position is None:
                del origins[origin]
                changed = True
            elif position != int(best.get("ns") or 0):
                origins[origin] = {"ns": position, "peer": peer}
                changed = True
    for origin, position in theirs.items():
        best = origins.get(origin)
        if not isinstance(best, dict) or position > int(best.get("ns") or 0):
            if best is None and len(origins) >= 4096:
                continue
            origins[origin] = {"ns": position, "peer": peer}
            changed = True
    if changed:
        payload = {"origins": origins}
        FleetSyncBestKnownV1.validate(payload)
        settings_ops.upsert_by_key(
            FLEET_SYNC_BEST_KNOWN_SET_ID, FLEET_SYNC_BEST_KNOWN_REVISION, scope,
            payload, org=org, state="raw")


def read_best_known(*, org: str = "machine") -> dict[str, dict]:
    """``{scope: {origin: {ns, peer}}}`` -- the best cursor any peer has
    reported per origin. This machine's lag in a scope is max over origins
    of (best ns - our cursor), computed at render (O(origins))."""
    members = settings_ops.read_owned_set(
        FLEET_SYNC_BEST_KNOWN_SET_ID, org=org,
        target_revision=FLEET_SYNC_BEST_KNOWN_REVISION,
    ).members
    return {
        member.key: dict((member.payload or {}).get("origins") or {})
        for member in members
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
        # Optional, so absent from the zero payload; carried through every
        # read-modify-write (record_bytes must not drop them).
        stored = row["payload"] or {}
        for name in _REDUCED:
            value = stored.get(name)
            if name == "behind_origin":
                if isinstance(value, str):
                    payload[name] = value
            elif not isinstance(value, bool) and isinstance(value, int) and value >= 0:
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
    local: Mapping[str, int] | None = None,
    at_ns: int | None = None,
    org: str = "machine",
) -> dict:
    """Reduce the map a peer just advertised for one scope, on arrival.

    ``watermarks`` is the peer's whole per-origin cursor map (W_P), sent on
    every pull. It is folded once and not stored whole (graph://6aa9bffc-ca9
    Record 3; pitfall graph://e6dba57c-f8b):

    - into this scope's best-known vector: per origin, the highest cursor any
      peer has reported (this machine's own lag = max over origins of best
      minus our cursor, at render);
    - against ``local`` (W_L, this machine's cursors now) into the peer's own
      lag: ``behind_ns``/``behind_origin`` = the worst per-origin gap
      ``W_L[o] - W_P[o]``, ``unreceived`` = origins we hold that it holds
      nothing of.

    ``frontier_ns`` (the minimum of the map) is kept as a diagnostic summary;
    no lag is computed from it. An empty map (a store mid-bootstrap claims
    nothing) leaves everything untouched.
    """
    key = peer_scope_key(peer_machine_public_key, scope)
    theirs = _clean_map(watermarks)
    if not theirs:
        return {}
    with _lock:
        payload = _read(key, org)
        payload["frontier_ns"] = min(theirs.values())
        if local is not None:
            payload.update(_trail(_clean_map(local), theirs))
        payload["observed_at_ns"] = (
            time.time_ns() if at_ns is None else int(at_ns)
        )
        written = _write(key, payload, org)
        _fold_best_known(scope, peer_machine_public_key, theirs, org)
        return written


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
    """``{peer: [{scope, frontier_ns, measured, behind_ns, behind_origin,
    unreceived, observed_at_ns, bytes_in, bytes_out}]}``.

    ``behind_*`` is how far the peer trailed this machine at its last pull,
    reduced per origin on arrival (graph://6aa9bffc-ca9 Record 3); this
    machine's own lag comes from read_best_known.
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
            "measured": "behind_ns" in payload,
            "behind_ns": int(payload.get("behind_ns") or 0),
            "behind_origin": str(payload.get("behind_origin") or ""),
            "unreceived": int(payload.get("unreceived") or 0),
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
            for name in _REDUCED:
                if name in payload:
                    updated[name] = payload[name]
            _write(member.key, updated, org)
            changed += 1
    return changed
