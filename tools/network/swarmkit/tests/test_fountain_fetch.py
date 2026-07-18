"""Fetcher tests over zero-latency in-process links.

Loopback is the *hostile* case for publisher egress — the block
scheduler measured 3× here because every leecher could drain the
publisher before have-maps propagated. The fountain transfer's bound
is structural (a monotonic cursor cannot re-serve a symbol), so these
tests pin it exactly where the old design failed.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tools.network.swarmkit.fetch import HandlerLink
from tools.network.swarmkit.fountain import FountainStore, source_symbols
from tools.network.swarmkit.fountain_fetch import (
    FountainFetchError,
    fountain_fetch,
)
from tools.network.swarmkit.fountain_protocol import fountain_handler

T = 1024
N_STRIPES = 8


class Node:
    """An in-process swarm node: store + handler, linkable by anyone."""

    def __init__(self, name, stripe):
        self.name = name
        self.store = FountainStore(stripe=stripe, n_stripes=N_STRIPES)
        self.handler = fountain_handler(self.store)

    def link(self):
        return HandlerLink(self.handler)


def links_to(others):
    return [(o.name, o.link()) for o in others]


def fetch(node, aid, others, **kw):
    kw.setdefault("idle_refresh", 0.05)
    kw.setdefault("timeout", 60.0)
    return fountain_fetch(node.store, aid, links_to(others), **kw)


class TestSingleSource:
    def test_one_leecher_completes_and_seeds(self):
        data = os.urandom(T * 300 + 17)

        async def run():
            pub = Node("publisher", 0)
            aid = pub.store.add_object(data, T)
            leecher = Node("leecher", 1)
            report = await fetch(leecher, aid, [pub])
            assert leecher.store.is_complete(aid)
            assert leecher.store.object(aid) == data
            k = source_symbols(pub.store.manifest(aid))
            # decoder-driven completion: K plus at most a few batches
            assert k <= report.symbols_held <= k + 96
            assert report.polluters == []
            # promoted: the leecher can now seed fresh symbols itself
            assert leecher.store.serve(aid, 3, []) != []
            return report

        asyncio.run(run())

    def test_no_valid_manifest_fails_and_closes_links(self):
        async def run():
            bystander = Node("bystander", 0)
            links = [("bystander", bystander.link())]
            with pytest.raises(FountainFetchError, match="manifest"):
                await fountain_fetch(
                    FountainStore(), "e" * 64, links, timeout=5.0,
                )
            assert all(link.closed for _, link in links)

        asyncio.run(run())

    def test_timeout_fails_closed_and_closes_links(self):
        data = os.urandom(T * 20)

        async def run():
            pub = Node("publisher", 0)
            aid = pub.store.add_object(data, T)
            # a peer that has the manifest but no symbols, and no one else
            dry = Node("dry", 1)
            dry.store.add_manifest(aid, pub.store.manifest(aid))
            leecher = Node("leecher", 2)
            links = links_to([dry])
            with pytest.raises(FountainFetchError, match="timed out"):
                await fountain_fetch(leecher.store, aid, links, timeout=0.5)
            assert not leecher.store.is_complete(aid)
            assert all(link.closed for _, link in links)

        asyncio.run(run())


class TestZeroLatencyEgress:
    def test_three_leechers_publisher_serves_about_once(self):
        """The flagship, on the links the block scheduler failed:
        3 concurrent leechers, in-process, zero latency. Publisher
        egress stays ≈1× because it is *arithmetic* — the cursor never
        re-serves — and the leechers' complementary slices make them
        finish off each other."""
        data = os.urandom(2 * 1024 * 1024 + 12345)

        async def run():
            pub = Node("publisher", 0)
            aid = pub.store.add_object(data, T)
            leechers = [Node(f"l{i}", i + 1) for i in range(3)]

            async def leech(me):
                others = [pub] + [l for l in leechers if l is not me]
                return await fetch(me, aid, others, timeout=120.0)

            reports = await asyncio.gather(*(leech(l) for l in leechers))
            k = source_symbols(pub.store.manifest(aid))

            for leecher in leechers:
                assert leecher.store.is_complete(aid)
                assert leecher.store.object(aid) == data

            # Structural invariant: the publisher NEVER re-served a
            # symbol — total packets out == distinct symbols out.
            m = pub.handler.metrics
            assert m.served_packets[aid] == len(m.served_ids[aid])

            # Publisher egress ≈ 1× on zero-latency links (the block
            # scheduler measured 3.0× here; acceptance bound 1.35×).
            distinct = len(m.served_ids[aid])
            assert distinct < 1.35 * k, f"publisher served {distinct/k:.2f}x"

            # It actually swarmed: every leecher took symbols from a
            # fellow leecher, and collectively the trade share covered
            # what the publisher did not serve.
            traded = 0
            for report in reports:
                from_peers = sum(
                    n for name, n in report.symbols_from.items()
                    if name != "publisher"
                )
                assert from_peers > 0, dict(report.symbols_from)
                traded += from_peers
            assert traded >= 3 * k - distinct - 3 * 96  # arrivals add up

        asyncio.run(run())


class TestMultiSeeder:
    def test_two_seeders_contribute_complementary_symbols(self):
        """One leecher, two complete seeders on distinct stripes: their
        deterministic streams cannot collide, so the aggregate distinct
        symbols served ≈ what one download needs — zero duplicate
        waste, which is the multi-seeder property the coordinator
        pinned."""
        data = os.urandom(T * 400 + 7)

        async def run():
            s1, s2 = Node("s1", 0), Node("s2", 1)
            aid = s1.store.add_object(data, T)
            s2.store.add_object(data, T)
            leecher = Node("leecher", 2)
            report = await fetch(leecher, aid, [s1, s2])

            assert leecher.store.object(aid) == data
            ids1 = s1.handler.metrics.served_ids[aid]
            ids2 = s2.handler.metrics.served_ids[aid]
            assert not ids1 & ids2, "stripes leaked overlapping symbols"
            # both actually contributed
            assert report.symbols_from["s1"] > 0
            assert report.symbols_from["s2"] > 0
            # aggregate ≈ needed: nothing wasted on duplicates
            k = source_symbols(s1.store.manifest(aid))
            assert len(ids1) + len(ids2) <= k + 96
            assert sum(report.duplicates.values()) == 0
            return report

        asyncio.run(run())

    def test_two_seeders_two_leechers_aggregate_stays_lean(self):
        data = os.urandom(T * 350 + 3)

        async def run():
            seeders = [Node("s1", 0), Node("s2", 1)]
            aid = seeders[0].store.add_object(data, T)
            seeders[1].store.add_object(data, T)
            leechers = [Node("l1", 2), Node("l2", 3)]

            async def leech(me):
                others = seeders + [l for l in leechers if l is not me]
                return await fetch(me, aid, others, timeout=120.0)

            reports = await asyncio.gather(*(leech(l) for l in leechers))
            k = source_symbols(seeders[0].store.manifest(aid))
            for leecher in leechers:
                assert leecher.store.object(aid) == data

            all_ids = [s.handler.metrics.served_ids[aid] for s in seeders]
            assert not all_ids[0] & all_ids[1]
            # two downloads, but trading keeps aggregate fresh symbols
            # near ONE object's worth (bound loose for scheduling noise)
            aggregate = len(all_ids[0]) + len(all_ids[1])
            assert aggregate < 1.5 * k, f"aggregate {aggregate/k:.2f}x"
            # no seeder ever re-served
            for s in seeders:
                m = s.handler.metrics
                assert m.served_packets[aid] == len(m.served_ids[aid])
            for report in reports:
                assert sum(report.symbols_from.values()) >= k

        asyncio.run(run())


class TestChurn:
    def test_seeder_death_mid_transfer_is_survived(self):
        data = os.urandom(T * 300 + 11)

        async def run():
            pub = Node("publisher", 0)
            aid = pub.store.add_object(data, T)
            flaky = Node("flaky", 1)
            flaky.store.add_object(data, T)

            calls = {"n": 0}
            inner = flaky.handler

            async def dying(token, message):
                calls["n"] += 1
                if calls["n"] > 4:
                    raise ConnectionError("flaky peer went away")
                return await inner(token, message)

            flaky_link = HandlerLink(dying)
            leecher = Node("leecher", 2)
            report = await fountain_fetch(
                leecher.store, aid,
                [("publisher", pub.link()), ("flaky", flaky_link)],
                idle_refresh=0.05, timeout=60.0,
            )
            assert leecher.store.object(aid) == data
            assert "flaky" in report.peer_errors
            # symbols are fungible: whatever flaky delivered still counted
            assert report.symbols_from["publisher"] > 0

        asyncio.run(run())


def corrupting(handler):
    """Wrap a handler: valid packet ids, poisoned symbol payloads."""

    async def evil(token, message):
        response = await handler(token, message)
        if b'"op":"fountain.symbols"' not in message:
            return response
        newline = response.find(b"\n")
        if newline < 0 or response[newline + 1:] == b"":
            return response
        body = bytearray(response[newline + 1:])
        # flip a byte inside every symbol payload, never the payload id
        for start in range(0, len(body), 4 + T):
            body[start + 10] ^= 0xFF
        return response[:newline + 1] + bytes(body)

    return evil


class TestPollution:
    def test_polluter_identified_and_fetch_completes_honestly(self):
        data = os.urandom(T * 200 + 5)

        async def run():
            pub = Node("publisher", 0)
            aid = pub.store.add_object(data, T)
            evil = Node("evil", 1)
            evil.store.add_object(data, T)
            leecher = Node("leecher", 2)
            report = await fountain_fetch(
                leecher.store, aid,
                [("publisher", pub.link()),
                 ("evil", HandlerLink(corrupting(evil.handler)))],
                idle_refresh=0.05, timeout=60.0,
            )
            # decode-verify caught the poison, leave-one-out named the
            # member, and the fetch still completed honestly
            assert report.polluters == ["evil"]
            assert leecher.store.is_complete(aid)
            assert leecher.store.object(aid) == data

        asyncio.run(run())

    def test_single_polluted_source_fails_closed(self):
        data = os.urandom(T * 60 + 5)

        async def run():
            evil = Node("evil", 0)
            aid = evil.store.add_object(data, T)
            leecher = Node("leecher", 1)
            links = [("evil", HandlerLink(corrupting(evil.handler)))]
            with pytest.raises(FountainFetchError) as excinfo:
                await fountain_fetch(
                    leecher.store, aid, links,
                    idle_refresh=0.05, timeout=60.0,
                )
            assert excinfo.value.polluters == ["evil"]
            # fail closed: nothing unverified was promoted or served
            assert not leecher.store.is_complete(aid)
            assert all(link.closed for _, link in links)

        asyncio.run(run())
