"""G2 acceptance — the real network stack (bead auto-25dz3, spec §8).

Every peer here is a genuine relaykit endpoint: a DirectChannelServer
with a ``tunnel:serve`` chain to the org root, dialed through the G1
``dial_peer`` chain, blocks moving as AES-256-GCM records over real
WebSockets. Pinned properties:

- **50 MB, 3 leechers, publisher uploads ≈1×**: leechers serve their
  growing stores to each other, so publisher block egress stays under
  1.5× the artifact size while every leecher completes and verifies.
- **Corruption is survivable**: a peer serving corrupted bytes is
  caught by per-block hashes, struck out, and the blocks re-fetched
  from an honest holder — the assembled artifact is byte-exact.
- **Rarest-first starves nothing**: with a slow full seeder and two
  fast partial seeders, the transfer completes at fast-peer speed,
  every block exactly once, single-holder blocks served by their only
  fast holder.
- **Links are ephemeral**: every server's open-connection count returns
  to zero once the transfer is done.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random

import pytest

from tools.network.relaykit.direct import DirectChannelServer
from tools.network.swarmkit import (
    BlockStore,
    dial_links,
    swarm_fetch,
    swarm_handler,
)

from .conftest import ORG, mint_node

BLOCK = 256 * 1024


class Peer:
    """A swarm peer: store + serving node + its dialable address."""

    def __init__(self, name, root, now):
        self.name = name
        self.store = BlockStore()
        self.key, self.cert = mint_node(root, now, name)
        self.handler = swarm_handler(self.store)
        self.server = DirectChannelServer(
            ORG, self.key, self.cert, self.handler, host="127.0.0.1", port=0
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


async def start_peers(root, now, names):
    return [await Peer(name, root, now).start() for name in names]


async def links_to(root, me, others):
    links, errors = await dial_links(
        [p.roster_row() for p in others], org=ORG, root_pub=root.public_hex
    )
    assert not errors, errors
    # name links by peer name, not pubkey, so reports read like the topology
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


class TestSwarmAcceptance:
    def test_three_leechers_50mb_publisher_uploads_about_once(self):
        """The flagship: 50 MB artifact, 3 concurrent leechers, block
        egress from the publisher measured under 1.5× the artifact."""
        size = 50 * 1024 * 1024
        data = os.urandom(size)
        digest = hashlib.sha256(data).hexdigest()

        async def run():
            from .conftest import ORG  # noqa: F401  (fixture-free async body)
            import time
            now = int(time.time())
            from tools.network.idkit import KeyPair
            root = KeyPair.generate()

            publisher, *leechers = await start_peers(
                root, now, ["publisher", "l1", "l2", "l3"]
            )
            aid = publisher.store.add_artifact(data, BLOCK)
            n_blocks = len(publisher.store.manifest(aid)["blocks"])

            async def leech(i, me):
                others = [publisher] + [p for p in leechers if p is not me]
                links = await links_to(root, me, others)
                return await swarm_fetch(
                    me.store, aid, links,
                    have_refresh=0.05, timeout=120.0, rng=random.Random(100 + i),
                )

            try:
                reports = await asyncio.gather(
                    *(leech(i, p) for i, p in enumerate(leechers))
                )
                for peer in leechers:
                    assembled = peer.store.assemble(aid)
                    assert hashlib.sha256(assembled).hexdigest() == digest
                    del assembled

                # Publisher block egress ≈ 1× (acceptance bound: < 1.5×).
                egress = publisher.handler.metrics.served_bytes[aid]
                assert egress < 1.5 * size, f"publisher egress {egress/size:.2f}x"

                # It actually swarmed: every leecher took blocks from a
                # fellow leecher, and collectively they moved at least as
                # many blocks between themselves as the publisher saved.
                for report in reports:
                    from_leechers = sum(
                        n for name, n in report.blocks_from.items()
                        if name != "publisher"
                    )
                    assert from_leechers > 0, dict(report.blocks_from)
                    assert sum(report.blocks_from.values()) == n_blocks

                await wait_links_closed([publisher] + leechers)
            finally:
                for peer in [publisher] + leechers:
                    await peer.stop()

        asyncio.run(run())

    def test_corrupt_block_detected_and_refetched_over_network(self):
        """A peer serving flipped bytes: hashes catch it, the swarm
        strikes it out, blocks come from the honest holder instead."""
        data = os.urandom(BLOCK * 12 + 999)
        digest = hashlib.sha256(data).hexdigest()

        async def run():
            import time
            from tools.network.idkit import KeyPair
            root = KeyPair.generate()
            now = int(time.time())

            honest, evil, fetcher = await start_peers(
                root, now, ["honest", "evil", "fetcher"]
            )
            aid = honest.store.add_artifact(data, BLOCK)
            evil.store.add_artifact(data, BLOCK)

            # Corrupt the wire, not the store: flip a byte in every
            # block response the evil node sends.
            inner = evil.handler

            async def corrupting(token, message):
                response = await inner(token, message)
                try:
                    request = json.loads(message)
                except ValueError:
                    return response
                if request.get("op") != "swarm.block":
                    return response
                newline = response.find(b"\n")
                body = bytearray(response[newline + 1:])
                body[0] ^= 0xFF
                return response[:newline + 1] + bytes(body)

            evil.server._handler = corrupting

            try:
                links = await links_to(root, fetcher, [evil, honest])
                report = await swarm_fetch(
                    fetcher.store, aid, links,
                    have_refresh=0.05, timeout=60.0, rng=random.Random(7),
                )
                assert hashlib.sha256(
                    fetcher.store.assemble(aid)
                ).hexdigest() == digest
                assert report.corrupt["evil"] >= 1
                assert report.blocks_from["evil"] == 0
                assert report.blocks_from["honest"] == 13
                await wait_links_closed([honest, evil, fetcher])
            finally:
                for peer in (honest, evil, fetcher):
                    await peer.stop()

        asyncio.run(run())

    def test_rarest_first_no_starvation_with_slow_peer(self):
        """Two fast partial seeders + one slow full seeder: the fetch
        finishes at fast-peer speed, every block exactly once, and each
        single-fast-holder block comes from its fast holder — nothing
        waits on (or starves behind) the slow peer."""
        n_blocks = 32
        data = os.urandom(BLOCK * (n_blocks - 1) + 4321)
        digest = hashlib.sha256(data).hexdigest()
        SLOW = 0.15

        async def run():
            import time
            from tools.network.idkit import KeyPair
            root = KeyPair.generate()
            now = int(time.time())

            fast_a, fast_b, slow, fetcher = await start_peers(
                root, now, ["fast_a", "fast_b", "slow", "fetcher"]
            )
            aid = slow.store.add_artifact(data, BLOCK)
            manifest = slow.store.manifest(aid)
            # fast_a: blocks 0..19, fast_b: 12..31 — 0..11 and 20..31
            # each have exactly one fast holder; 12..19 have two.
            for peer, held in ((fast_a, range(0, 20)), (fast_b, range(12, 32))):
                peer.store.add_manifest(aid, manifest)
                for i in held:
                    peer.store.add_block(aid, i, slow.store.get_block(aid, i))

            inner = slow.handler

            async def slow_handler(token, message):
                await asyncio.sleep(SLOW)
                return await inner(token, message)

            slow.server._handler = slow_handler

            try:
                links = await links_to(root, fetcher, [fast_a, fast_b, slow])
                started = time.perf_counter()
                report = await swarm_fetch(
                    fetcher.store, aid, links,
                    have_refresh=0.05, timeout=60.0, rng=random.Random(7),
                )
                duration = time.perf_counter() - started
                assert hashlib.sha256(
                    fetcher.store.assemble(aid)
                ).hexdigest() == digest

                # No starvation: every block exactly once, and the slow
                # peer never became the path of least resistance.
                indices = sorted(i for i, _ in report.fetch_order)
                assert indices == list(range(n_blocks))
                fast_count = (report.blocks_from["fast_a"]
                              + report.blocks_from["fast_b"])
                assert fast_count >= n_blocks - 4, dict(report.blocks_from)
                # All-through-slow would cost ≥ n_blocks * SLOW ≈ 4.8 s
                # sequential; the swarm must beat that decisively.
                assert duration < n_blocks * SLOW / 2, f"{duration:.2f}s"

                # Rarest-first: a single-fast-holder block is always
                # served by that fast holder (or, rarely, the slow full
                # seeder) — never missing; two-holder blocks split.
                by_index = dict(report.fetch_order)
                for i in range(0, 12):
                    assert by_index[i] in ("fast_a", "slow")
                for i in range(20, 32):
                    assert by_index[i] in ("fast_b", "slow")
                await wait_links_closed([fast_a, fast_b, slow, fetcher])
            finally:
                for peer in (fast_a, fast_b, slow, fetcher):
                    await peer.stop()

        asyncio.run(run())
