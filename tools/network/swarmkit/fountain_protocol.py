"""Fountain wire protocol — symbols over the relaykit handler seam.

Two request/response ops ride the same E2E channel contract as every
other endpoint (``async handler(token, message) -> response``), so a
node serves symbols over any rung of the connectivity chain:

    {"v": 1, "op": "fountain.manifest", "artifact": <64 hex>}
        → JSON {"v": 1, "ok": true, "manifest": {...}} | {"ok": false, ...}

    {"v": 1, "op": "fountain.symbols", "artifact": <64 hex>,
     "count": 1..64, "exclude": [[start, end], ...]?}
        → binary  canonical_json({"v": 1, "ok": true, "n": k}) ‖ "\\n"
                  ‖ k serialized packets (each 4 + symbol_size bytes)
        | JSON {"ok": false, ...}

``exclude`` is the requester's holdings as packed-id ranges; the store
serves only symbols outside it (fresh stripe symbols from a complete
holder, stored packets round-robin from a partial one). ``n`` may be
less than ``count`` — including 0, "nothing new for you right now",
which a fetcher treats as idle, not as an error.

There is no have-map and no want-list: symbols are interchangeable, so
"what do you have" collapses into "give me anything I don't".
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Dict, List, Optional, Set

from tools.network.idkit import canonical_json

from .fountain import (
    FountainError,
    FountainStore,
    check_ranges,
    packet_id,
    packet_length,
)
from .protocol import _err, _valid_artifact

FOUNTAIN_PROTOCOL_VERSION = 1
FOUNTAIN_OPS = ("fountain.manifest", "fountain.symbols")
MAX_SYMBOL_BATCH = 64


class FountainMetrics:
    """What a serving node observed — the egress ledger for symbols.

    ``served_bytes`` is the publisher-egress instrumentation the ≈1×
    acceptance reads; ``served_ids`` keeps *distinct* symbols served.
    One node never re-serves (``served_packets == len(served_ids)``,
    cursor arithmetic) — but under stripe collisions *different*
    seeders serve the same ids, so swarm-wide the ≈1× claim is about
    the distinct union, and total bandwidth degrades ~N_collision×.
    Comparing ``served_ids`` across seeders is how collision (and the
    v2 dynamic-reassignment trigger) is detected.
    """

    def __init__(self) -> None:
        self.served_bytes: Counter = Counter()      # artifact -> bytes out
        self.served_packets: Counter = Counter()    # artifact -> packets out
        self.served_ids: Dict[str, Set[int]] = {}   # artifact -> distinct ids

    def note_serve(self, artifact_id: str, packets: List[bytes]) -> None:
        self.served_bytes[artifact_id] += sum(len(p) for p in packets)
        self.served_packets[artifact_id] += len(packets)
        ids = self.served_ids.setdefault(artifact_id, set())
        ids.update(packet_id(p) for p in packets)


def fountain_handler(store: FountainStore, *,
                     metrics: Optional[FountainMetrics] = None,
                     fallback=None):
    """A relaykit channel handler serving *store*'s symbols to the swarm.

    Non-fountain messages go to *fallback* (another handler — e.g. the
    block-era ``swarm_handler`` or an application handler) when given,
    else get a JSON error. Composes exactly like every other handler on
    the seam.
    """
    metrics = metrics if metrics is not None else FountainMetrics()

    async def handler(token: str, message: bytes) -> bytes:
        try:
            request = json.loads(message)
        except (ValueError, UnicodeDecodeError):
            request = None
        if (
            not isinstance(request, dict)
            or request.get("op") not in FOUNTAIN_OPS
        ):
            if fallback is not None:
                return await fallback(token, message)
            return _err("bad-request")
        if request.get("v") != FOUNTAIN_PROTOCOL_VERSION:
            return _err("bad-version")
        artifact_id = request.get("artifact")
        if not _valid_artifact(artifact_id):
            return _err("bad-artifact")

        manifest = store.manifest(artifact_id)

        if request["op"] == "fountain.manifest":
            if manifest is None:
                return _err("unknown-artifact")
            return canonical_json(
                {"v": FOUNTAIN_PROTOCOL_VERSION, "ok": True, "manifest": manifest}
            )

        # op == "fountain.symbols"
        if manifest is None:
            return _err("unknown-artifact")
        count = request.get("count")
        if (
            not isinstance(count, int) or isinstance(count, bool)
            or not 1 <= count <= MAX_SYMBOL_BATCH
        ):
            return _err("bad-count")
        try:
            exclude = check_ranges(request.get("exclude", []))
        except FountainError:
            return _err("bad-exclude")
        packets = store.serve(artifact_id, count, exclude)
        metrics.note_serve(artifact_id, packets)
        header = canonical_json(
            {"v": FOUNTAIN_PROTOCOL_VERSION, "ok": True, "n": len(packets)}
        )
        return header + b"\n" + b"".join(packets)

    handler.metrics = metrics
    return handler


def parse_symbols_response(response: bytes,
                           manifest: dict) -> Optional[List[bytes]]:
    """Split a ``fountain.symbols`` response into packets, or None.

    None covers every refusal or malformed shape — the fetcher treats
    them all as "this peer can't serve symbols right now". An ``ok``
    response whose body length disagrees with its packet count is
    malformed, not partial.
    """
    newline = response.find(b"\n")
    if newline < 0:
        return None
    try:
        header = json.loads(response[:newline])
    except ValueError:
        return None
    if (
        not isinstance(header, dict)
        or header.get("v") != FOUNTAIN_PROTOCOL_VERSION
        or header.get("ok") is not True
    ):
        return None
    n = header.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        return None
    body = response[newline + 1:]
    step = packet_length(manifest)
    if len(body) != n * step:
        return None
    return [body[i:i + step] for i in range(0, len(body), step)]
