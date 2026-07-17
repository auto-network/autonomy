"""Wire protocol over the handler seam: ops, refusals, composition."""

from __future__ import annotations

import asyncio
import json
import os

from tools.network.idkit import canonical_json
from tools.network.swarmkit.protocol import (
    PROTOCOL_VERSION,
    parse_block_response,
    swarm_handler,
)
from tools.network.swarmkit.store import (
    BlockStore,
    bitmap_hex_to_indices,
    indices_to_bitmap_hex,
)

BLOCK = 4096


def seeded():
    data = os.urandom(BLOCK * 3 + 17)
    store = BlockStore()
    aid = store.add_artifact(data, BLOCK)
    return data, store, aid


def call(handler, request) -> bytes:
    payload = canonical_json(request) if isinstance(request, dict) else request
    return asyncio.run(handler("t", payload))


class TestOps:
    def test_manifest_roundtrip(self):
        _, store, aid = seeded()
        handler = swarm_handler(store)
        resp = json.loads(call(handler, {
            "v": 1, "op": "swarm.manifest", "artifact": aid,
        }))
        assert resp["ok"] and resp["manifest"] == store.manifest(aid)

    def test_have_bitmap_and_want_recorded(self):
        _, store, aid = seeded()
        handler = swarm_handler(store)
        want = indices_to_bitmap_hex({1, 3}, 4)
        resp = json.loads(call(handler, {
            "v": 1, "op": "swarm.have", "artifact": aid, "want": want,
        }))
        assert bitmap_hex_to_indices(resp["have"], 4) == {0, 1, 2, 3}
        assert handler.metrics.wants_seen[aid] == {1, 3}

    def test_block_served_and_metered(self):
        data, store, aid = seeded()
        handler = swarm_handler(store)
        resp = call(handler, {"v": 1, "op": "swarm.block", "artifact": aid,
                              "index": 3})
        block = parse_block_response(resp, 3)
        assert block == data[3 * BLOCK:]
        assert handler.metrics.served_bytes[aid] == len(block)
        assert handler.metrics.block_serves[aid][3] == 1

    def test_partial_store_advertises_only_what_it_holds(self):
        data, seed, aid = seeded()
        store = BlockStore()
        store.add_manifest(aid, seed.manifest(aid))
        store.add_block(aid, 2, seed.get_block(aid, 2))
        handler = swarm_handler(store)
        resp = json.loads(call(handler, {"v": 1, "op": "swarm.have",
                                         "artifact": aid}))
        assert bitmap_hex_to_indices(resp["have"], 4) == {2}
        miss = json.loads(call(handler, {"v": 1, "op": "swarm.block",
                                         "artifact": aid, "index": 0}))
        assert miss == {"v": 1, "ok": False, "error": "missing-block"}


class TestRefusals:
    def test_unknown_artifact(self):
        handler = swarm_handler(BlockStore())
        for op in ("swarm.manifest", "swarm.have"):
            resp = json.loads(call(handler, {"v": 1, "op": op,
                                             "artifact": "ab" * 32}))
            assert resp == {"v": 1, "ok": False, "error": "unknown-artifact"}

    def test_malformed_requests(self):
        _, store, aid = seeded()
        handler = swarm_handler(store)
        cases = [
            (b"\xff\xfe not json", "bad-request"),
            ({"v": 2, "op": "swarm.have", "artifact": aid}, "bad-version"),
            ({"v": 1, "op": "swarm.have", "artifact": "short"}, "bad-artifact"),
            ({"v": 1, "op": "swarm.have", "artifact": aid, "want": "zz"},
             "bad-want"),
            ({"v": 1, "op": "swarm.block", "artifact": aid, "index": "0"},
             "bad-index"),
            ({"v": 1, "op": "swarm.block", "artifact": aid, "index": 99},
             "bad-index"),
        ]
        for request, error in cases:
            resp = json.loads(call(handler, request))
            assert resp["error"] == error, request

    def test_non_swarm_falls_back_or_errors(self):
        _, store, aid = seeded()

        async def fallback(token, message):
            return b"app:" + message

        composed = swarm_handler(store, fallback=fallback)
        assert call(composed, {"op": "fetch", "v": 1}).startswith(b"app:")
        bare = swarm_handler(store)
        assert json.loads(call(bare, {"op": "fetch", "v": 1}))["error"] == "bad-request"

    def test_block_response_parser_rejects_junk(self):
        good = canonical_json({"v": PROTOCOL_VERSION, "ok": True, "index": 1}) + b"\nBB"
        assert parse_block_response(good, 1) == b"BB"
        assert parse_block_response(good, 2) is None          # wrong index
        assert parse_block_response(b"no newline", 1) is None
        assert parse_block_response(b"not json\nBB", 1) is None
        refused = canonical_json({"v": 1, "ok": False, "error": "missing-block"})
        assert parse_block_response(refused, 1) is None
