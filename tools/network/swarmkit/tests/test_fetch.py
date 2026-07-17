"""Fetcher scheduling: rarest-first, corruption recovery, want-list flow.

In-process links (:class:`HandlerLink`) — no sockets; the network-stack
acceptance lives in ``test_swarm_integration.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import random

import pytest

from tools.network.idkit import canonical_json
from tools.network.swarmkit import (
    BlockStore,
    HandlerLink,
    SwarmFetchError,
    swarm_fetch,
    swarm_handler,
)
from tools.network.swarmkit.protocol import PROTOCOL_VERSION

BLOCK = 4096


def make_artifact(n_blocks=16, tail=17):
    data = os.urandom(BLOCK * (n_blocks - 1) + tail)
    seed = BlockStore()
    aid = seed.add_artifact(data, BLOCK)
    return data, seed, aid


def partial_store(seed, aid, indices):
    store = BlockStore()
    store.add_manifest(aid, seed.manifest(aid))
    for i in indices:
        store.add_block(aid, i, seed.get_block(aid, i))
    return store


def corrupting_handler(inner, indices=None):
    """Wrap a swarm handler: flip a byte in served blocks (all or *indices*)."""

    async def handler(token, message):
        response = await inner(token, message)
        request = json.loads(message)
        if request.get("op") != "swarm.block":
            return response
        if indices is not None and request["index"] not in indices:
            return response
        newline = response.find(b"\n")
        if newline < 0 or json.loads(response[:newline]).get("ok") is not True:
            return response
        body = bytearray(response[newline + 1:])
        body[0] ^= 0xFF
        return response[:newline + 1] + bytes(body)

    return handler


def run_fetch(store, aid, links, **kw):
    kw.setdefault("have_refresh", 0.02)
    kw.setdefault("timeout", 10.0)
    kw.setdefault("rng", random.Random(7))
    return asyncio.run(swarm_fetch(store, aid, links, **kw))


class TestFetch:
    def test_single_seeder_complete_and_link_closed(self):
        data, seed, aid = make_artifact()
        store = BlockStore()
        link = HandlerLink(swarm_handler(seed))
        report = run_fetch(store, aid, [("seed", link)])
        assert store.assemble(aid) == data
        assert report.blocks_from["seed"] == 16
        assert link.closed

    def test_blocks_never_fetched_twice(self):
        data, seed, aid = make_artifact()
        store = BlockStore()
        links = [(f"s{i}", HandlerLink(swarm_handler(seed))) for i in range(3)]
        report = run_fetch(store, aid, links)
        assert store.assemble(aid) == data
        indices = [i for i, _ in report.fetch_order]
        assert sorted(indices) == list(range(16))  # each exactly once
        assert sum(report.blocks_from.values()) == 16

    def test_rarest_first_prefers_underreplicated(self):
        data, seed, aid = make_artifact(n_blocks=16)
        # rare: blocks 0..3 exist on one peer; common: 4..15 on all three
        a = partial_store(seed, aid, set(range(0, 4)) | set(range(4, 16)))
        b = partial_store(seed, aid, set(range(4, 16)))
        c = partial_store(seed, aid, set(range(4, 16)))
        store = BlockStore()
        report = run_fetch(store, aid, [
            ("a", HandlerLink(swarm_handler(a))),
            ("b", HandlerLink(swarm_handler(b))),
            ("c", HandlerLink(swarm_handler(c))),
        ])
        assert store.assemble(aid) == data
        # every rare block came from the only holder
        by_index = dict(report.fetch_order)
        assert all(by_index[i] == "a" for i in range(4))
        # a's session spent its picks on the rare blocks first
        a_order = [i for i, n in report.fetch_order if n == "a"]
        assert set(a_order[:4]) == {0, 1, 2, 3}

    def test_corrupt_block_detected_and_refetched(self):
        data, seed, aid = make_artifact()
        store = BlockStore()
        evil = corrupting_handler(swarm_handler(seed), indices={5})
        report = run_fetch(store, aid, [
            ("evil", HandlerLink(evil)),
            ("honest", HandlerLink(swarm_handler(seed))),
        ])
        assert store.assemble(aid) == data
        assert report.corrupt["evil"] >= 1
        assert dict(report.fetch_order)[5] == "honest"

    def test_fully_corrupt_peer_dropped_but_fetch_completes(self):
        data, seed, aid = make_artifact()
        store = BlockStore()
        evil = corrupting_handler(swarm_handler(seed))
        report = run_fetch(store, aid, [
            ("evil", HandlerLink(evil)),
            ("honest", HandlerLink(swarm_handler(seed))),
        ], strike_limit=3)
        assert store.assemble(aid) == data
        assert report.corrupt["evil"] == 3       # dropped at the limit
        assert report.blocks_from["evil"] == 0
        assert report.blocks_from["honest"] == 16

    def test_forged_manifest_rejected_next_peer_wins(self):
        data, seed, aid = make_artifact()

        async def liar(token, message):
            request = json.loads(message)
            if request.get("op") == "swarm.manifest":
                forged = dict(seed.manifest(aid))
                forged["size"] = forged["size"] + BLOCK
                forged["blocks"] = list(forged["blocks"]) + ["ab" * 32]
                return canonical_json(
                    {"v": PROTOCOL_VERSION, "ok": True, "manifest": forged}
                )
            return await swarm_handler(seed)(token, message)

        store = BlockStore()
        report = run_fetch(store, aid, [
            ("liar", HandlerLink(liar)),
            ("honest", HandlerLink(swarm_handler(seed))),
        ])
        assert store.assemble(aid) == data
        assert "manifest" in report.peer_errors["liar"]

    def test_no_source_raises(self):
        _, seed, aid = make_artifact()
        empty = BlockStore()
        empty.add_manifest(aid, seed.manifest(aid))
        store = BlockStore()
        with pytest.raises(SwarmFetchError):
            run_fetch(store, aid, [("empty", HandlerLink(swarm_handler(empty)))],
                      timeout=0.5)

    def test_dead_link_routed_around(self):
        data, seed, aid = make_artifact()

        async def dead(token, message):
            raise ConnectionError("wire cut")

        store = BlockStore()
        report = run_fetch(store, aid, [
            ("dead", HandlerLink(dead)),
            ("honest", HandlerLink(swarm_handler(seed))),
        ])
        assert store.assemble(aid) == data
        assert "dead" in report.peer_errors

    def test_resume_fetches_only_missing_blocks(self):
        data, seed, aid = make_artifact()
        store = partial_store(seed, aid, set(range(0, 10)))
        counting = swarm_handler(seed)
        report = run_fetch(store, aid, [("seed", HandlerLink(counting))])
        assert store.assemble(aid) == data
        assert sum(report.blocks_from.values()) == 6
        served = counting.metrics.block_serves[aid]
        assert all(i >= 10 for i in served)
