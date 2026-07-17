"""Swarm wire protocol — served through the relaykit handler seam (G2).

Three request/response ops ride the existing E2E channel contract
(``async handler(token, message) -> response``), so a node serves swarm
traffic over ANY rung of the connectivity chain — direct listener, peer
relay, or central floor — without knowing which carried the request:

    {"v": 1, "op": "swarm.manifest", "artifact": <64 hex>}
        → JSON {"v": 1, "ok": true, "manifest": {...}} | {"ok": false, ...}

    {"v": 1, "op": "swarm.have", "artifact": <64 hex>, "want": <hex bitmap>?}
        → JSON {"v": 1, "ok": true, "have": <hex bitmap>}

    {"v": 1, "op": "swarm.block", "artifact": <64 hex>, "index": i}
        → binary  canonical_json({"v": 1, "ok": true, "index": i}) ‖ "\n" ‖ block
        | JSON {"ok": false, ...}

``want`` is the fetcher's want-list (advisory in v1: recorded by the
serving side's metrics as the seam where push-notification lands later;
scheduling today is pull — the fetcher re-polls have-maps while peers
acquire blocks).

Responses never distinguish "unknown artifact" from "not holding that
block" beyond what a peer could infer anyway; a serving peer only ever
sends bytes it can verify against its own manifest.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Dict, Optional

from tools.network.idkit import canonical_json

from .store import BlockStore, bitmap_hex_to_indices, indices_to_bitmap_hex

PROTOCOL_VERSION = 1
OPS = ("swarm.manifest", "swarm.have", "swarm.block")


class SwarmMetrics:
    """What a serving node observed — egress accounting for the swarm.

    ``served_bytes``/``block_serves`` are the publisher-egress
    instrumentation the ≈1×-upload acceptance measures; ``wants_seen``
    keeps the advisory want-lists observable.
    """

    def __init__(self) -> None:
        self.served_bytes: Counter = Counter()      # artifact -> block bytes out
        self.block_serves: Dict[str, Counter] = {}  # artifact -> index -> count
        self.wants_seen: Dict[str, set] = {}        # artifact -> union of wants

    def note_serve(self, artifact_id: str, index: int, n: int) -> None:
        self.served_bytes[artifact_id] += n
        self.block_serves.setdefault(artifact_id, Counter())[index] += 1

    def note_want(self, artifact_id: str, indices: set) -> None:
        self.wants_seen.setdefault(artifact_id, set()).update(indices)


def _err(error: str) -> bytes:
    return canonical_json({"v": PROTOCOL_VERSION, "ok": False, "error": error})


def _valid_artifact(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def swarm_handler(store: BlockStore, *, metrics: Optional[SwarmMetrics] = None,
                  fallback=None):
    """A relaykit channel handler serving *store* to the swarm.

    Non-swarm messages go to *fallback* (another handler) when given —
    the composition point for nodes that serve share-link traffic and
    swarm traffic behind one seam — else get a JSON error.
    """
    metrics = metrics if metrics is not None else SwarmMetrics()

    async def handler(token: str, message: bytes) -> bytes:
        try:
            request = json.loads(message)
        except (ValueError, UnicodeDecodeError):
            request = None
        if (
            not isinstance(request, dict)
            or request.get("op") not in OPS
        ):
            if fallback is not None:
                return await fallback(token, message)
            return _err("bad-request")
        if request.get("v") != PROTOCOL_VERSION:
            return _err("bad-version")
        artifact_id = request.get("artifact")
        if not _valid_artifact(artifact_id):
            return _err("bad-artifact")

        manifest = store.manifest(artifact_id)
        op = request["op"]

        if op == "swarm.manifest":
            if manifest is None:
                return _err("unknown-artifact")
            return canonical_json(
                {"v": PROTOCOL_VERSION, "ok": True, "manifest": manifest}
            )

        if op == "swarm.have":
            if manifest is None:
                return _err("unknown-artifact")
            total = len(manifest["blocks"])
            want = request.get("want")
            if want is not None:
                try:
                    metrics.note_want(artifact_id, bitmap_hex_to_indices(want, total))
                except Exception:
                    return _err("bad-want")
            bitmap = indices_to_bitmap_hex(store.have(artifact_id), total)
            return canonical_json(
                {"v": PROTOCOL_VERSION, "ok": True, "have": bitmap}
            )

        # op == "swarm.block"
        index = request.get("index")
        if manifest is None:
            return _err("unknown-artifact")
        if not isinstance(index, int) or not 0 <= index < len(manifest["blocks"]):
            return _err("bad-index")
        block = store.get_block(artifact_id, index)
        if block is None:
            return _err("missing-block")
        metrics.note_serve(artifact_id, index, len(block))
        header = canonical_json({"v": PROTOCOL_VERSION, "ok": True, "index": index})
        return header + b"\n" + block

    handler.metrics = metrics
    return handler


def parse_block_response(response: bytes, expected_index: int):
    """Split a ``swarm.block`` response; returns block bytes or None.

    None covers every refusal or malformed shape — the fetcher treats
    them all as "this peer can't serve that block right now".
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
        or header.get("v") != PROTOCOL_VERSION
        or header.get("ok") is not True
        or header.get("index") != expected_index
    ):
        return None
    return response[newline + 1:]
