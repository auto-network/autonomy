"""Fountain acceptance — the real network stack (bead auto-0m2kp, §8 rev 2).

Every peer is a genuine relaykit endpoint: a DirectChannelServer with a
``tunnel:serve`` chain to the org root, dialed through the G1
``dial_peer`` chain, symbols moving as AES-256-GCM records over real
WebSockets. Pinned properties:

- **50 MB, 3 leechers, publisher serves ≈1× — structurally**: the
  publisher's distinct-symbol egress stays ≈K whatever the link
  latency (the zero-latency companion test in ``test_fountain_fetch``
  pins the case the block scheduler failed at 3×; this one pins the
  same arithmetic over real sockets).
- **Multi-seeder complementarity**: two complete seeders on distinct
  roster stripes feed leechers disjoint symbol sets — aggregate fresh
  symbols ≈ one object, zero duplicate waste.
- **Pollution is survivable and attributable**: a member serving valid
  packet ids over poisoned payloads is caught by decode-verify,
  identified by leave-one-out, and the fetch completes honestly from
  the honest members.
- **Links are ephemeral**: every server's open-connection count
  returns to zero once the transfer is done.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time

from tools.network.idkit import KeyPair
from tools.network.relaykit.direct import DirectChannelServer
from tools.network.swarmkit import (
    FountainStore,
    dial_links,
    fountain_fetch,
    fountain_handler,
    roster_stripe,
    source_symbols,
)

from .conftest import ORG, mint_node

SYMBOL = 8192


class Peer:
    """A fountain peer: striped store + serving node + dialable address."""

    def __init__(self, name, key, cert, stripe, handler_wrap=None):
        self.name = name
        self.key, self.cert = key, cert
        self.store = FountainStore(stripe=stripe)
        self.handler = fountain_handler(self.store)
        handler = handler_wrap(self.handler) if handler_wrap else self.handler
        self.server = DirectChannelServer(
            ORG, self.key, self.cert, handler, host="127.0.0.1", port=0
        )

    async def start(self):
        await self.server.start()
        return self

    @property
    def addr(self):
        return f"ws://127.0.0.1:{self.server.port}"

    def roster_row(self):
        return {"node": self.key.public_hex, "direct_addrs": [self.addr]}

    async def stop(self):
        await self.server.stop()


async def start_swarm(root, now, names, wraps=None):
    """Start peers with stripes derived the production way: from the
    stable ordering of the (test) roster's member keys."""
    identities = {name: mint_node(root, now, name) for name in names}
    roster = [key.public_hex for key, _ in identities.values()]
    return [
        await Peer(
            name, key, cert, roster_stripe(key.public_hex, roster),
            (wraps or {}).get(name),
        ).start()
        for name, (key, cert) in identities.items()
    ]


async def links_to(root, others):
    links, errors = await dial_links(
        [p.roster_row() for p in others], org=ORG, root_pub=root.public_hex
    )
    assert not errors, errors
    by_node = {p.key.public_hex: p.name for p in others}
    return [(by_node[node], link) for node, link in links]


async def wait_links_closed(peers, deadline=5.0):
    """Idle-out assertion: every server drains to zero open connections."""
    for _ in range(int(deadline / 0.05)):
        if all(p.server.connection_count == 0 for p in peers):
            return
        await asyncio.sleep(0.05)
    counts = {p.name: p.server.connection_count for p in peers}
    raise AssertionError(f"links outlived the transfer: {counts}")


class TestFountainAcceptance:
    def test_three_leechers_50mb_publisher_serves_about_once(self):
        """The flagship: 50 MB object, 3 concurrent leechers over real
        websockets — every leecher decodes and verifies, the publisher's
        distinct-symbol egress stays ≈1×, and it never re-serves."""
        size = 50 * 1024 * 1024
        data = os.urandom(size)
        digest = hashlib.sha256(data).hexdigest()

        async def run():
            now = int(time.time())
            root = KeyPair.generate()
            publisher, *leechers = await start_swarm(
                root, now, ["publisher", "l1", "l2", "l3"]
            )
            aid = publisher.store.add_object(data, SYMBOL)
            k = source_symbols(publisher.store.manifest(aid))

            async def leech(me):
                others = [publisher] + [p for p in leechers if p is not me]
                links = await links_to(root, others)
                return await fountain_fetch(
                    me.store, aid, links, idle_refresh=0.05, timeout=180.0,
                )

            try:
                reports = await asyncio.gather(*(leech(p) for p in leechers))
                for peer in leechers:
                    obj = peer.store.object(aid)
                    assert hashlib.sha256(obj).hexdigest() == digest
                    del obj

                metrics = publisher.handler.metrics
                distinct = len(metrics.served_ids[aid])
                # never re-served a symbol — the structural invariant
                assert metrics.served_packets[aid] == distinct
                # publisher egress ≈ 1× (acceptance bound 1.35×)
                assert distinct < 1.35 * k, f"publisher {distinct/k:.2f}x"

                # it swarmed: every leecher traded with fellow leechers
                for report in reports:
                    from_peers = sum(
                        n for name, n in report.symbols_from.items()
                        if name != "publisher"
                    )
                    assert from_peers > 0, dict(report.symbols_from)
                    assert report.polluters == []

                await wait_links_closed([publisher] + leechers)
            finally:
                for peer in [publisher] + leechers:
                    await peer.stop()

        asyncio.run(run())

    def test_multi_seeder_disjoint_contributions_over_network(self):
        """Two complete seeders on roster stripes, two leechers: the
        seeders' served symbol sets never intersect and their aggregate
        stays near one object's worth — no duplicate waste."""
        size = 8 * 1024 * 1024
        data = os.urandom(size)
        digest = hashlib.sha256(data).hexdigest()

        async def run():
            now = int(time.time())
            root = KeyPair.generate()
            s1, s2, l1, l2 = await start_swarm(
                root, now, ["s1", "s2", "l1", "l2"]
            )
            aid = s1.store.add_object(data, SYMBOL)
            s2.store.add_object(data, SYMBOL)
            k = source_symbols(s1.store.manifest(aid))
            leechers = [l1, l2]

            async def leech(me):
                others = [s1, s2] + [p for p in leechers if p is not me]
                links = await links_to(root, others)
                return await fountain_fetch(
                    me.store, aid, links, idle_refresh=0.05, timeout=120.0,
                )

            try:
                await asyncio.gather(*(leech(p) for p in leechers))
                for peer in leechers:
                    assert hashlib.sha256(
                        peer.store.object(aid)
                    ).hexdigest() == digest

                ids1 = s1.handler.metrics.served_ids[aid]
                ids2 = s2.handler.metrics.served_ids[aid]
                assert not ids1 & ids2, "stripes overlapped"
                assert len(ids1) > 0 and len(ids2) > 0
                aggregate = len(ids1) + len(ids2)
                assert aggregate < 1.5 * k, f"aggregate {aggregate/k:.2f}x"
                for seeder in (s1, s2):
                    m = seeder.handler.metrics
                    assert m.served_packets[aid] == len(m.served_ids[aid])

                await wait_links_closed([s1, s2, l1, l2])
            finally:
                for peer in (s1, s2, l1, l2):
                    await peer.stop()

        asyncio.run(run())

    def test_polluting_member_identified_over_network(self):
        """A member serving poisoned symbol payloads on the wire:
        decode-verify catches it, leave-one-out names it, the fetch
        completes honestly from the honest seeder."""
        size = 2 * 1024 * 1024
        data = os.urandom(size)
        digest = hashlib.sha256(data).hexdigest()

        def corrupting(inner):
            async def evil(token, message):
                response = await inner(token, message)
                if b'"op":"fountain.symbols"' not in message:
                    return response
                newline = response.find(b"\n")
                body = bytearray(response[newline + 1:])
                for start in range(0, len(body), 4 + SYMBOL):
                    body[start + 10] ^= 0xFF
                return response[:newline + 1] + bytes(body)
            return evil

        async def run():
            now = int(time.time())
            root = KeyPair.generate()
            honest, evil, fetcher = await start_swarm(
                root, now, ["honest", "evil", "fetcher"],
                wraps={"evil": corrupting},
            )
            aid = honest.store.add_object(data, SYMBOL)
            evil.store.add_object(data, SYMBOL)

            try:
                links = await links_to(root, [honest, evil])
                report = await fountain_fetch(
                    fetcher.store, aid, links,
                    idle_refresh=0.05, timeout=120.0,
                )
                assert report.polluters == ["evil"]
                assert hashlib.sha256(
                    fetcher.store.object(aid)
                ).hexdigest() == digest
                await wait_links_closed([honest, evil, fetcher])
            finally:
                for peer in (honest, evil, fetcher):
                    await peer.stop()

        asyncio.run(run())
