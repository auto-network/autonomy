"""Handler tests — fountain ops, refusals, metrics, seam composition."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from tools.network.idkit import canonical_json
from tools.network.swarmkit import BlockStore, swarm_handler
from tools.network.swarmkit.fountain import FountainStore, packet_id
from tools.network.swarmkit.fountain_protocol import (
    FOUNTAIN_PROTOCOL_VERSION,
    MAX_SYMBOL_BATCH,
    fountain_handler,
    parse_symbols_response,
)

T = 256


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def seeded():
    store = FountainStore(stripe=0, n_stripes=1)
    data = os.urandom(T * 9 + 55)
    aid = store.add_object(data, T)
    return store, aid, data


def req(op, **fields):
    return canonical_json({"v": FOUNTAIN_PROTOCOL_VERSION, "op": op, **fields})


class TestOps:
    def test_manifest_roundtrip(self, seeded):
        store, aid, _ = seeded
        handler = fountain_handler(store)
        response = run(handler("t", req("fountain.manifest", artifact=aid)))
        data = json.loads(response)
        assert data["ok"] is True
        assert data["manifest"] == store.manifest(aid)

    def test_symbols_roundtrip_and_metrics(self, seeded):
        store, aid, _ = seeded
        handler = fountain_handler(store)
        response = run(handler("t", req(
            "fountain.symbols", artifact=aid, count=7, exclude=[],
        )))
        packets = parse_symbols_response(response, store.manifest(aid))
        assert packets is not None and len(packets) == 7
        assert handler.metrics.served_packets[aid] == 7
        assert handler.metrics.served_bytes[aid] == 7 * (4 + T)
        assert len(handler.metrics.served_ids[aid]) == 7

    def test_exclude_honored_on_the_wire(self, seeded):
        store, aid, _ = seeded
        handler = fountain_handler(store)
        first = parse_symbols_response(run(handler("t", req(
            "fountain.symbols", artifact=aid, count=5, exclude=[],
        ))), store.manifest(aid))
        ids = sorted(packet_id(p) for p in first)
        exclude = [[i, i + 1] for i in ids]
        second = parse_symbols_response(run(handler("t", req(
            "fountain.symbols", artifact=aid, count=5, exclude=exclude,
        ))), store.manifest(aid))
        assert not {packet_id(p) for p in second} & set(ids)

    def test_zero_available_is_ok_not_error(self):
        store = FountainStore()
        data = os.urandom(T * 3)
        seeder = FountainStore()
        aid = seeder.add_object(data, T)
        store.add_manifest(aid, seeder.manifest(aid))
        handler = fountain_handler(store)
        response = run(handler("t", req(
            "fountain.symbols", artifact=aid, count=4, exclude=[],
        )))
        packets = parse_symbols_response(response, seeder.manifest(aid))
        assert packets == []


class TestRefusals:
    def test_bad_requests(self, seeded):
        store, aid, _ = seeded
        handler = fountain_handler(store)

        def err_of(message):
            return json.loads(run(handler("t", message))).get("error")

        assert err_of(b"not json") == "bad-request"
        assert err_of(canonical_json({"op": "nope"})) == "bad-request"
        assert err_of(canonical_json(
            {"v": 9, "op": "fountain.symbols", "artifact": aid}
        )) == "bad-version"
        assert err_of(req("fountain.symbols", artifact="short")) == "bad-artifact"
        assert err_of(req("fountain.manifest", artifact="a" * 64)) == "unknown-artifact"
        assert err_of(req("fountain.symbols", artifact="a" * 64, count=1)) == "unknown-artifact"
        for count in (0, -1, MAX_SYMBOL_BATCH + 1, "3", True, None):
            assert err_of(req(
                "fountain.symbols", artifact=aid, count=count,
            )) == "bad-count", count
        assert err_of(req(
            "fountain.symbols", artifact=aid, count=1, exclude=[[3, 1]],
        )) == "bad-exclude"

    def test_parse_rejects_malformed_responses(self, seeded):
        store, aid, _ = seeded
        m = store.manifest(aid)
        ok = canonical_json({"v": 1, "ok": True, "n": 1})
        assert parse_symbols_response(b"no newline", m) is None
        assert parse_symbols_response(b"{bad json}\nx", m) is None
        assert parse_symbols_response(
            canonical_json({"v": 1, "ok": False}) + b"\n", m) is None
        assert parse_symbols_response(ok + b"\n" + b"x", m) is None  # short body
        assert parse_symbols_response(
            canonical_json({"v": 1, "ok": True, "n": -1}) + b"\n", m) is None
        assert parse_symbols_response(
            canonical_json({"v": 1, "ok": True, "n": True}) + b"\n" + b"\0" * (4 + T),
            m) is None


class TestSeamComposition:
    def test_fountain_chains_to_swarm_and_app(self, seeded):
        fstore, aid, data = seeded
        bstore = BlockStore()
        bid = bstore.add_artifact(data, T)

        async def app(token, message):
            return b"app:" + message

        handler = fountain_handler(
            fstore, fallback=swarm_handler(bstore, fallback=app)
        )
        # fountain op served by the fountain layer
        response = run(handler("t", req("fountain.manifest", artifact=aid)))
        assert json.loads(response)["ok"] is True
        # block op falls through to the swarm layer
        response = run(handler("t", canonical_json(
            {"v": 1, "op": "swarm.manifest", "artifact": bid}
        )))
        assert json.loads(response)["ok"] is True
        # everything else reaches the application
        assert run(handler("t", b"hello")) == b"app:hello"

    def test_without_fallback_non_fountain_is_refused(self, seeded):
        store, _, _ = seeded
        handler = fountain_handler(store)
        assert json.loads(run(handler("t", b"hello")))["error"] == "bad-request"
