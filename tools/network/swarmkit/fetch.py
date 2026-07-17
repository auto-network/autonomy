"""Swarm fetcher — want-list scheduling, rarest-first, verified blocks (G2).

The fetcher holds the want-list (blocks it still needs) and polls each
peer's have-map; every peer session independently pulls the **rarest**
wanted block its peer can serve (availability counted across all
sessions' latest have-maps, ties broken randomly), so concurrent
fetchers naturally spread across under-replicated blocks and pick the
well-replicated ones up from each other — that is what holds publisher
egress near 1× on a multi-consumer artifact.

Trust model: none in the transport. Every block re-hashes on receipt
(:meth:`BlockStore.add_block`); a corrupt block strikes the peer,
returns the index to the want-list, and any other holder serves it.
Enough strikes drop the peer for the rest of the fetch.

Links are ephemeral: every session closes its channel when the fetch
completes, errors out, or its peer is dropped — no link outlives the
transfer.

The org roster is the tracker (§8): callers build the peer list from
the ledger's member projection + the registry's reachability hints and
dial through the G1 fallback chain (:func:`dial_links`); the fetcher
itself only ever sees established links.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from tools.network.idkit import canonical_json

from .protocol import PROTOCOL_VERSION, parse_block_response
from .store import BlockError, BlockStore, bitmap_hex_to_indices, indices_to_bitmap_hex

DEFAULT_HAVE_REFRESH = 0.5
DEFAULT_STRIKE_LIMIT = 3
DEFAULT_BLOCKS_PER_POLL = 4


class SwarmFetchError(Exception):
    """The swarm could not complete the artifact."""

    def __init__(self, message: str, peer_errors: Optional[dict] = None):
        self.peer_errors = dict(peer_errors or {})
        detail = "; ".join(f"[{n}] {e}" for n, e in self.peer_errors.items())
        super().__init__(message + (f" ({detail})" if detail else ""))


class HandlerLink:
    """A zero-transport link straight onto a handler (tests, loopback)."""

    def __init__(self, handler, token: str = "swarm"):
        self._handler = handler
        self._token = token
        self.closed = False

    async def request(self, payload: bytes) -> bytes:
        if self.closed:
            raise ConnectionError("link closed")
        return await self._handler(self._token, payload)

    async def close(self) -> None:
        self.closed = True


class ChannelLink:
    """One in-flight request at a time over an established E2E channel."""

    def __init__(self, channel):
        self._channel = channel
        self._lock = asyncio.Lock()

    async def request(self, payload: bytes) -> bytes:
        async with self._lock:
            await self._channel.send_message(payload)
            return await self._channel.recv_message()

    async def close(self) -> None:
        await self._channel.close()


async def dial_links(peers: Sequence[dict], *, org: str, root_pub: str,
                     attempt_timeout: float = 3.0,
                     now: Optional[int] = None) -> Tuple[list, dict]:
    """Dial each roster peer through the G1 fallback chain.

    *peers* rows are ``{"node": <pub>, "direct_addrs": [...],
    "relays": [...], "floor": (url, token) | None}`` — the shape the
    ledger roster + reachability hints produce. Returns ``(links,
    errors)`` where links are ``(node, ChannelLink)`` for every peer
    that answered; unreachable peers land in *errors* instead of
    failing the fetch (the swarm routes around them).
    """
    from tools.network.relaykit.dialer import dial_peer

    links: list = []
    errors: dict = {}

    async def one(peer: dict):
        try:
            result = await dial_peer(
                org=org, root_pub=root_pub, target_pub=peer["node"],
                direct_addrs=list(peer.get("direct_addrs") or []),
                relays=list(peer.get("relays") or []),
                floor=peer.get("floor"),
                attempt_timeout=attempt_timeout, now=now,
            )
            links.append((peer["node"], ChannelLink(result.channel)))
        except Exception as exc:
            errors[peer["node"]] = repr(exc)

    await asyncio.gather(*(one(p) for p in peers))
    return links, errors


@dataclass
class FetchReport:
    """How the swarm delivered an artifact."""

    artifact_id: str
    blocks_from: Counter = field(default_factory=Counter)   # peer -> blocks
    corrupt: Counter = field(default_factory=Counter)       # peer -> bad blocks
    fetch_order: List[Tuple[int, str]] = field(default_factory=list)
    peer_errors: Dict[str, str] = field(default_factory=dict)
    duration: float = 0.0


class _SwarmState:
    """Shared scheduling state across peer sessions."""

    def __init__(self, artifact_id: str, want: Set[int], total: int,
                 rng: random.Random, strike_limit: int):
        self.artifact_id = artifact_id
        self.want = want
        self.total = total
        self.rng = rng
        self.strike_limit = strike_limit
        self.inflight: Set[int] = set()
        self.peer_have: Dict[str, Set[int]] = {}
        self.avail: Dict[int, Set[str]] = {}
        # Per-fetcher tie-break weights. Concurrent fetchers drawing
        # fresh random picks from the same equal-rarity pool collide
        # birthday-style as the pool shrinks (measured: ~25% duplicate
        # publisher serves); a fixed random ranking per fetcher sends
        # each one to its own corner of the pool instead.
        self.weight = [rng.random() for _ in range(total)]
        self.strikes: Counter = Counter()
        self.done = asyncio.Event()
        self.progress = 0  # bumps on every stored block (poll pacing)
        self.report = FetchReport(artifact_id)
        if not want:
            self.done.set()

    def note_have(self, name: str, have: Set[int]) -> None:
        self.peer_have[name] = have
        for i in have:
            self.avail.setdefault(i, set()).add(name)

    def choose(self, name: str) -> Optional[int]:
        """Rarest wanted block this peer can serve.

        Key, in order: rarity across the swarm's latest have-maps; then
        whether *this* peer is the smallest-library holder of the block
        (when several connected peers hold it, pull from the one with
        the fewest blocks — the full-copy peer is everyone's only
        source of still-rare blocks, so its link should spend last on
        blocks others already carry); then this fetcher's fixed random
        ranking, so concurrent fetchers spread over equal-rarity pools
        instead of colliding."""
        have = self.peer_have.get(name, ())
        my_size = len(have)
        best: Optional[int] = None
        best_key = None
        for i in have:
            if i not in self.want or i in self.inflight:
                continue
            holders = self.avail.get(i, ())
            smallest = my_size <= min(
                (len(self.peer_have.get(h, ())) for h in holders), default=my_size
            )
            key = (len(holders), 0 if smallest else 1, self.weight[i])
            if best_key is None or key < best_key:
                best, best_key = i, key
        return best

    def note_got(self, index: int, name: str) -> None:
        self.want.discard(index)
        self.progress += 1
        self.report.blocks_from[name] += 1
        self.report.fetch_order.append((index, name))
        if not self.want:
            self.done.set()

    def note_bad(self, index: int, name: str, *, corrupt: bool) -> None:
        """A failed block attempt: strike the peer, stop asking it for
        that index (its advertisement was wrong or its bytes were), and
        leave the index wanted for everyone else."""
        self.strikes[name] += 1
        if corrupt:
            self.report.corrupt[name] += 1
        self.peer_have.get(name, set()).discard(index)
        self.avail.get(index, set()).discard(name)

    def dropped(self, name: str) -> bool:
        return self.strikes[name] >= self.strike_limit


def _have_request(state: _SwarmState) -> bytes:
    return canonical_json({
        "v": PROTOCOL_VERSION, "op": "swarm.have", "artifact": state.artifact_id,
        "want": indices_to_bitmap_hex(state.want, state.total),
    })


def _block_request(artifact_id: str, index: int) -> bytes:
    return canonical_json({
        "v": PROTOCOL_VERSION, "op": "swarm.block",
        "artifact": artifact_id, "index": index,
    })


def _parse_have(response: bytes, total: int) -> Set[int]:
    import json

    data = json.loads(response)
    if not isinstance(data, dict):
        raise BlockError("have response is not an object")
    if data.get("ok") is not True:
        if data.get("error") == "unknown-artifact":
            # A fellow leecher that hasn't learned the manifest yet —
            # an empty have-map, not a broken peer; keep polling.
            return set()
        raise BlockError(f"have refused: {data!r}")
    return bitmap_hex_to_indices(data["have"], total)


async def _fetch_manifest(store: BlockStore, artifact_id: str, links,
                          errors: dict) -> Optional[dict]:
    """First peer whose manifest verifies against the artifact id wins."""
    import json

    request = canonical_json({
        "v": PROTOCOL_VERSION, "op": "swarm.manifest", "artifact": artifact_id,
    })
    for name, link in links:
        try:
            data = json.loads(await link.request(request))
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise BlockError(f"manifest refused: {data!r}")
            return store.add_manifest(artifact_id, data.get("manifest"))
        except Exception as exc:
            errors[name] = f"manifest: {exc!r}"
    return None


async def _session(store: BlockStore, state: _SwarmState, name: str, link,
                   have_refresh: float, blocks_per_poll: int) -> None:
    progress_seen = -1
    try:
        while not state.done.is_set() and not state.dropped(name):
            have = _parse_have(
                await link.request(_have_request(state)), state.total
            )
            state.note_have(name, have)
            # Budgeted drain: at most *blocks_per_poll* blocks between
            # have-map refreshes. Rarity counts are only as fresh as the
            # last poll — an unbounded drain would let one fast source
            # serve half the artifact while the swarm's real
            # distribution runs ahead of the scheduler's picture of it
            # (measured: publisher egress 1.74× without the cap, ~1.1×
            # with it, three concurrent leechers on loopback).
            # Endgame taper: as the want-set shrinks, stale rarity info
            # costs proportionally more (the last blocks are the ones
            # every fetcher is racing for), so drain fewer blocks
            # between refreshes.
            budget = max(1, min(blocks_per_poll, len(state.want) // 8))
            idle = False
            for _ in range(budget):
                if state.done.is_set() or state.dropped(name):
                    break
                index = state.choose(name)
                if index is None:
                    idle = True
                    break
                state.inflight.add(index)
                try:
                    response = await link.request(
                        _block_request(state.artifact_id, index)
                    )
                    block = parse_block_response(response, index)
                    if block is not None and store.add_block(
                        state.artifact_id, index, block
                    ):
                        state.note_got(index, name)
                    else:
                        state.note_bad(index, name, corrupt=block is not None)
                finally:
                    state.inflight.discard(index)
            if state.done.is_set() or state.dropped(name):
                break
            if idle and state.progress == progress_seen:
                # This peer had nothing we want AND the swarm is quiet:
                # wait before re-polling. While blocks are landing
                # elsewhere the map is going stale by the millisecond,
                # so an idle-but-progressing session re-polls at once —
                # leaving it asleep just routes more pulls to whichever
                # peer never idles (the publisher).
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(state.done.wait(), have_refresh)
            progress_seen = state.progress
    except Exception as exc:
        state.report.peer_errors[name] = repr(exc)
    finally:
        # Ephemeral links: nothing outlives the transfer.
        with contextlib.suppress(Exception):
            await link.close()


async def swarm_fetch(
    store: BlockStore,
    artifact_id: str,
    links: Sequence[tuple],
    *,
    have_refresh: float = DEFAULT_HAVE_REFRESH,
    strike_limit: int = DEFAULT_STRIKE_LIMIT,
    blocks_per_poll: int = DEFAULT_BLOCKS_PER_POLL,
    timeout: Optional[float] = None,
    rng: Optional[random.Random] = None,
) -> FetchReport:
    """Pull *artifact_id* into *store* from established peer *links*.

    *links* is ``[(name, link), ...]`` — anything with the
    ``request``/``close`` shape (:class:`ChannelLink` from
    :func:`dial_links`, :class:`HandlerLink` in-process). All links are
    closed by the time this returns, success or not.

    Raises :class:`SwarmFetchError` if no peer supplies a valid
    manifest, the swarm collectively cannot complete the artifact, or
    *timeout* elapses.
    """
    import time

    links = list(links)
    rng = rng or random.Random()
    started = time.perf_counter()

    async def run() -> FetchReport:
        errors: dict = {}
        manifest = store.manifest(artifact_id)
        if manifest is None:
            manifest = await _fetch_manifest(store, artifact_id, links, errors)
            if manifest is None:
                raise SwarmFetchError("no peer supplied a valid manifest", errors)
        total = len(manifest["blocks"])
        want = set(range(total)) - store.have(artifact_id)
        state = _SwarmState(artifact_id, want, total, rng, strike_limit)
        state.report.peer_errors.update(errors)

        async def prime(name, link):
            """Initial have-map exchange (the bitfield-on-connect step):
            rarity counts must span the whole swarm before the first
            pick, or early scheduling degenerates to random."""
            with contextlib.suppress(Exception):
                state.note_have(name, _parse_have(
                    await link.request(_have_request(state)), total
                ))

        await asyncio.gather(*(prime(name, link) for name, link in links))
        await asyncio.gather(*(
            _session(store, state, name, link, have_refresh, blocks_per_poll)
            for name, link in links
        ))
        if not store.is_complete(artifact_id):
            raise SwarmFetchError(
                f"artifact incomplete: {len(state.want)} of {total} blocks missing",
                state.report.peer_errors,
            )
        state.report.duration = time.perf_counter() - started
        return state.report

    async def close_all() -> None:
        for _, link in links:
            with contextlib.suppress(Exception):
                await link.close()

    try:
        if timeout is None:
            return await run()
        return await asyncio.wait_for(run(), timeout)
    except asyncio.TimeoutError:
        await close_all()
        raise SwarmFetchError(f"swarm fetch timed out after {timeout}s") from None
    except BaseException:
        # Ephemeral links, even in failure: errors raised BEFORE the
        # sessions exist (e.g. no peer supplied a valid manifest) must
        # not leak the channels the sessions would have closed.
        await close_all()
        raise
