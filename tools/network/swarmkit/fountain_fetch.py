"""Fountain fetcher — collect symbols until decode, verify, fail closed.

No want-list, no rarest-first, no endgame: every symbol any peer emits
is useful until the decoder says done. Each link session loops "send me
up to *n* symbols I don't hold" (holdings travel as packed-id exclude
ranges); arrivals feed one incremental RaptorQ decoder; the moment it
yields the object, the bytes are hashed against the manifest commitment
— match promotes the object into the store (this node seeds now),
mismatch is **pollution** and the fetch fails closed: no unverified
byte is ever returned or served.

Pollution identification (bead auto-0m2kp): peers are authenticated
org members, so a polluter is nameable. Leave-one-out over the peers
that contributed symbols — re-decode from every packet *except* one
suspect's, topping up from the remaining links when that leaves fewer
than the decoder needs. The exclusion that yields a hash-verified
decode names the polluter (and completes the fetch honestly); if no
single exclusion does, the artifact stays failed-closed with the
contributor set reported. Attribution is to the *serving* member —
an org member vouches for every symbol it serves, wherever it learned
it; per-symbol publisher signatures are the v2 hardening that would
localise poison through honest relays too.

Egress note (why the ≈1× bound is structural now): a leecher's intake
from the publisher is whatever share of its collection the publisher's
monotonic cursor happened to serve — every publisher symbol is fresh,
so publisher egress equals *distinct symbols contributed*, latency
cannot inflate it, and leechers holding complementary cursor slices
complete each other by trading. The block scheduler's bound was a
latency accident; this one is arithmetic.

Links are ephemeral: every link is closed by the time the fetch
returns, on every path — success, timeout, pollution, or error.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from tools.network.idkit import canonical_json

from .fountain import Decoder, FountainStore, packet_id, source_symbols
from .fountain_protocol import FOUNTAIN_PROTOCOL_VERSION, parse_symbols_response

DEFAULT_SYMBOL_BATCH = 32
DEFAULT_IDLE_REFRESH = 0.2
IDENTIFY_TOPUP_MARGIN = 128     # extra symbols allowed per suspect trial


class FountainFetchError(Exception):
    """The swarm could not deliver a verified object."""

    def __init__(self, message: str, peer_errors: Optional[dict] = None,
                 polluters: Optional[Sequence[str]] = None):
        self.peer_errors = dict(peer_errors or {})
        self.polluters = list(polluters or [])
        detail = "; ".join(f"[{n}] {e}" for n, e in self.peer_errors.items())
        if self.polluters:
            detail = f"polluters={self.polluters}" + (f"; {detail}" if detail else "")
        super().__init__(message + (f" ({detail})" if detail else ""))


@dataclass
class FountainReport:
    """How the swarm delivered (or failed to deliver) an artifact."""

    artifact_id: str
    symbols_from: Counter = field(default_factory=Counter)  # peer -> accepted
    duplicates: Counter = field(default_factory=Counter)    # peer -> already-held
    peer_errors: Dict[str, str] = field(default_factory=dict)
    polluters: List[str] = field(default_factory=list)
    symbols_held: int = 0
    duration: float = 0.0


class _FountainState:
    """Shared fetch state across link sessions."""

    def __init__(self, artifact_id: str, manifest: dict):
        self.artifact_id = artifact_id
        self.manifest = manifest
        self.k = source_symbols(manifest)
        self.decoder = Decoder.with_defaults(
            manifest["size"], manifest["symbol_size"]
        )
        self.source_of: Dict[int, str] = {}   # packed id -> serving peer
        self.done = asyncio.Event()
        self.poisoned = False
        self.data: Optional[bytes] = None
        self.progress = 0                     # bumps on every accepted packet
        self.report = FountainReport(artifact_id)

    def held(self) -> int:
        return len(self.source_of)

    def accept(self, store: FountainStore, name: str, packet: bytes) -> bool:
        """Store + attribute one arrival; returns False on duplicate."""
        if not store.add_packet(self.artifact_id, packet):
            self.report.duplicates[name] += 1
            return False
        self.source_of[packet_id(packet)] = name
        self.report.symbols_from[name] += 1
        self.progress += 1
        return True

    def settle(self, store: FountainStore, decoded: bytes) -> bool:
        """Hash-verify a decode result; True = object accepted."""
        if hashlib.sha256(decoded).hexdigest() == self.manifest["object"]:
            store.promote(self.artifact_id, decoded)
            self.data = decoded
            return True
        self.poisoned = True
        return False


def _symbols_request(state: _FountainState, store: FountainStore,
                     count: int) -> bytes:
    return canonical_json({
        "v": FOUNTAIN_PROTOCOL_VERSION, "op": "fountain.symbols",
        "artifact": state.artifact_id, "count": count,
        "exclude": store.held_ranges(state.artifact_id),
    })


async def _fetch_manifest(store: FountainStore, artifact_id: str, links,
                          errors: dict) -> Optional[dict]:
    """First peer whose manifest verifies against the artifact id wins."""
    request = canonical_json({
        "v": FOUNTAIN_PROTOCOL_VERSION, "op": "fountain.manifest",
        "artifact": artifact_id,
    })
    for name, link in links:
        try:
            data = json.loads(await link.request(request))
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise ValueError(f"manifest refused: {data!r}")
            return store.add_manifest(artifact_id, data.get("manifest"))
        except Exception as exc:
            errors[name] = f"manifest: {exc!r}"
    return None


async def _session(store: FountainStore, state: _FountainState, name: str,
                   link, batch: int, idle_refresh: float) -> None:
    """One peer link: pull symbols until the artifact settles.

    A peer with nothing new (n=0, or all-duplicate batches) idles —
    but only when the whole swarm is quiet; while other sessions are
    landing symbols, this peer's next batch is going stale by the
    millisecond, so re-poll at once (same pacing lesson the block
    scheduler learned on loopback).
    """
    progress_seen = -1
    try:
        while not state.done.is_set():
            # Cooperative fairness point. Over zero-latency in-process
            # links a request never touches the event loop, so without
            # this one session would drain its peer to completion
            # before the others ever ran — the exact publisher-egress
            # failure this transfer exists to rule out. Free on real
            # sockets (they yield anyway).
            await asyncio.sleep(0)
            if state.done.is_set():
                break
            count = min(batch, max(1, state.k + 2 - state.held()))
            response = await link.request(_symbols_request(state, store, count))
            packets = parse_symbols_response(response, state.manifest)
            if packets is None:
                state.report.peer_errors[name] = "malformed symbols response"
                return
            fresh = 0
            for packet in packets:
                if state.accept(store, name, packet) and state.data is None:
                    result = state.decoder.decode(packet)
                    if result is not None and not state.done.is_set():
                        state.settle(store, bytes(result))
                        state.done.set()
                        return
                    fresh += 1
            if not fresh and state.progress == progress_seen:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(state.done.wait(), idle_refresh)
            progress_seen = state.progress
    except Exception as exc:
        state.report.peer_errors[name] = repr(exc)


async def _identify_polluters(store: FountainStore, state: _FountainState,
                              links) -> None:
    """Leave-one-out re-decode: name the member whose symbols poison.

    For each contributing peer, rebuild a decoder from everyone else's
    packets, topping up from the other links if that undershoots what
    the decoder needs. A hash-verified decode convicts the excluded
    peer and completes the fetch honestly. Extra symbols pulled during
    one trial stay in the pool for the next.
    """
    aid = state.artifact_id
    contributors = [n for n, c in state.report.symbols_from.items() if c > 0]
    live = {
        name: link for name, link in links
        if name not in state.report.peer_errors
    }
    if len(contributors) == 1:
        # One source, bad decode: nobody else to blame.
        state.report.polluters = contributors
        return

    for suspect in contributors:
        trial = Decoder.with_defaults(
            state.manifest["size"], state.manifest["symbol_size"]
        )
        result = None
        for packed, name in sorted(state.source_of.items()):
            if name == suspect:
                continue
            packet = store.packet(aid, packed)
            if packet is None:
                continue
            result = trial.decode(packet)
            if result is not None:
                break
        budget = state.report.symbols_from[suspect] + IDENTIFY_TOPUP_MARGIN
        while result is None and budget > 0:
            pulled = 0
            for name, link in live.items():
                if name == suspect or result is not None:
                    continue
                count = min(DEFAULT_SYMBOL_BATCH, budget)
                if count < 1:
                    break
                try:
                    packets = parse_symbols_response(
                        await link.request(_symbols_request(state, store, count)),
                        state.manifest,
                    )
                except Exception as exc:
                    state.report.peer_errors[name] = repr(exc)
                    live.pop(name, None)
                    continue
                if packets is None:
                    state.report.peer_errors[name] = "malformed symbols response"
                    live.pop(name, None)
                    continue
                for packet in packets:
                    if not state.accept(store, name, packet):
                        continue
                    pulled += 1
                    budget -= 1
                    result = trial.decode(packet)
                    if result is not None:
                        break
            if not pulled:
                break
        if result is not None and state.settle(store, bytes(result)):
            state.report.polluters = [suspect]
            return
        # else: excluding this suspect still decoded wrong (or not at
        # all) — an innocent peer; move to the next suspect.


async def fountain_fetch(
    store: FountainStore,
    artifact_id: str,
    links: Sequence[tuple],
    *,
    symbol_batch: int = DEFAULT_SYMBOL_BATCH,
    idle_refresh: float = DEFAULT_IDLE_REFRESH,
    timeout: Optional[float] = None,
) -> FountainReport:
    """Pull *artifact_id* into *store* from established peer *links*.

    *links* is ``[(name, link), ...]`` — anything with the
    ``request``/``close`` shape (``ChannelLink`` from ``dial_links``,
    ``HandlerLink`` in-process). All links are closed by the time this
    returns, success or not.

    Returns a :class:`FountainReport`; if pollution was detected and a
    single member identified, the fetch still completes honestly and
    ``report.polluters`` names it (the caller's revocation hook).
    Raises :class:`FountainFetchError` if no peer supplies a valid
    manifest, the object cannot be decoded and verified, or *timeout*
    elapses.
    """
    links = list(links)
    started = time.perf_counter()

    async def run() -> FountainReport:
        errors: dict = {}
        manifest = store.manifest(artifact_id)
        if manifest is None:
            manifest = await _fetch_manifest(store, artifact_id, links, errors)
            if manifest is None:
                raise FountainFetchError("no peer supplied a valid manifest", errors)
        state = _FountainState(artifact_id, manifest)
        state.report.peer_errors.update(errors)

        if store.is_complete(artifact_id):
            state.data = store.object(artifact_id)
        else:
            await asyncio.gather(*(
                _session(store, state, name, link, symbol_batch, idle_refresh)
                for name, link in links
            ))
            if state.data is None and state.poisoned:
                await _identify_polluters(store, state, links)

        state.report.symbols_held = state.held()
        state.report.duration = time.perf_counter() - started
        if state.data is None:
            if state.poisoned:
                raise FountainFetchError(
                    "decoded object failed hash verification (pollution); "
                    "failing closed",
                    state.report.peer_errors,
                    state.report.polluters
                    or [f"unresolved among {sorted(state.report.symbols_from)}"],
                )
            raise FountainFetchError(
                f"object incomplete: {state.held()} symbols held, "
                f"K={state.k}", state.report.peer_errors,
            )
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
        raise FountainFetchError(
            f"fountain fetch timed out after {timeout}s"
        ) from None
    finally:
        # Ephemeral links on EVERY path — the auto-25dz3 review lesson.
        await close_all()
