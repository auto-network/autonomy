# swarmkit — content-addressed swarm bulk fetch (G2)

Spec `graph://eb245082-b76` §8, bead `auto-25dz3`. Builds on relaykit
(G1): large artifacts move as fixed-size **blocks** pulled from any org
peer that holds them, over the same E2E channels the fallback chain
establishes — direct, peer relay, or central floor; the application
seam cannot tell which.

## Trust model

The transport is untrusted end to end; two hashes carry all integrity:

- **Artifact id** = SHA-256 of the canonical-JSON manifest. A manifest
  fetched from any peer verifies against the id alone
  (`store.add_manifest`); a forged manifest cannot name the same id.
- **Block hashes** live in the manifest. Every block re-hashes on
  receipt (`store.add_block`); corrupt bytes are refused, the sender is
  struck (3 strikes drops it for the fetch), and the index returns to
  the want-list for any other holder to serve.

Who may *serve* is the same question as G1: swarm traffic rides the
channel contract, so a serving node authenticates with its
`tunnel:serve` chain like any other endpoint.

## Discovery — the roster is the tracker (no DHT)

An org is a closed, authenticated world: the ledger's member/live-keys
projection says who exists, the registry's reachability hints say where
to dial them. `dial_links()` turns those rows into established channels
through the G1 chain (`dial_peer`: direct → peer relay → floor).
There is nothing to crawl and nobody anonymous to ask — no DHT inside
an org, by design.

## Protocol (over the `handler(token, message)` seam)

    {"v":1, "op":"swarm.manifest", "artifact": <64 hex>}
    {"v":1, "op":"swarm.have",     "artifact": ..., "want": <hex bitmap>?}
    {"v":1, "op":"swarm.block",    "artifact": ..., "index": i}

Have-maps are LSB-first hex bitmaps (a 10 GiB artifact at the 256 KiB
default block size is a 5 KiB bitmap). Block responses are a canonical
JSON header line + raw bytes. `swarm_handler(store, fallback=...)`
composes with an application handler behind one seam;
`handler.metrics` (`SwarmMetrics`) meters block egress — the
countersigned-usage seam (§10) and the publisher-egress acceptance
both read it.

The `want` field is the fetcher's want-list. In v1 it is advisory
(recorded, not acted on): scheduling is pull — the fetcher re-polls
have-maps while peers acquire blocks. Server-initiated HAVE push (the
BitTorrent HAVE message; needs channel push, which the request/response
seam doesn't do yet) is the v2 lever that would erase the remaining
~0.25× duplicate publisher egress measured on loopback.

## Scheduling (`swarm_fetch`)

One session per peer link, one request in flight per session:

1. **Prime**: initial have-map exchange across all links (bitfield-on-
   connect) — rarity counts must span the swarm before the first pick.
2. **Pick** = min `(rarity, holder-rank, weight)`:
   - *rarity* — count of peers advertising the block (rarest-first);
   - *holder-rank* — prefer pulling from the smallest-library holder,
     so the full-copy peer's link spends last on blocks other peers
     already carry;
   - *weight* — a per-fetcher fixed random ranking, so concurrent
     fetchers spread across an equal-rarity pool instead of colliding
     birthday-style.
3. **Budgeted drain**: at most `blocks_per_poll` (4) blocks between
   have-map refreshes, tapering to 1 as the want-set shrinks —
   measured on loopback, an unbounded drain let the publisher serve
   1.74× the artifact; the budget + taper hold it at ~1.25×.
4. An idle session (peer had nothing wanted) re-polls immediately
   while the swarm is progressing, and backs off `have_refresh` only
   when it is quiet.
5. **Ephemeral links**: every session closes its channel when the
   fetch completes, its peer is dropped, or the fetch errors — pinned
   by a connection-count-returns-to-zero assertion.

In-flight indices are excluded from other sessions' picks, so a block
is never fetched twice by one fetcher (asserted); across independent
fetchers the weight ranking keeps duplication low.

## Library spike (spec §13 Q6) — why hand-rolled

Measured in the agent container (2026-07-17, decision recorded as a
comment on `graph://eb245082-b76`):

| candidate | verdict |
|---|---|
| iroh 1.1.0 (PyPI) | installs clean; bindings expose the QUIC endpoint layer ONLY (no blobs/bitswap) — the swarm logic would be hand-rolled anyway; 50 MB bi-stream measured **10 MB/s** (FFI-bound, buffer-size invariant); parallel identity+transport universe next to G1 |
| py-libp2p 0.7.0 | ships bitswap now, but pins `fastecdsa==2.3.2` (source-only, needs gcc+GMP — not installable in the agent container) and is trio-based against our asyncio stack |
| hand-rolled over relaykit | existing E2E channel measured **667 MB/s** single channel / 871 MB/s ×4 on loopback; sha256 verify 2341 MB/s (~4% of one channel); inherits G1 auth + fallback chain natively |

iroh remains the candidate for a real UDP hole-punching *transport*
rung later (`direct.py` v1 boundary); it is not a swarm layer for us.

## Tests

```bash
pytest tools/network/swarmkit/tests/
```

36 unit tests (store/content addressing, protocol ops + refusals,
scheduler properties incl. corruption recovery, forged manifest,
resume) + 3 network acceptance tests over real relaykit stacks
(`test_swarm_integration.py`):

- **50 MB, 3 concurrent leechers**: all verify byte-exact; publisher
  block egress asserted < 1.5× (measured ~1.22–1.33× across seeds;
  peer-to-peer share ~57–60%).
- **Corrupt peer**: flipped bytes on the wire caught by hash, peer
  struck out, blocks re-fetched from the honest holder.
- **Slow peer, rarest-first**: two fast partial seeders + slow full
  seeder — completes at fast-peer speed, every block exactly once,
  single-fast-holder blocks served by their fast holder.

## v1 boundaries, deliberate

- Stores are in-memory; a disk-backed store slots behind the same
  `BlockStore` surface when artifact sizes demand.
- Pull-only have-map polling (no server push) — see want-list note.
- `choose()` scans candidate blocks per pick (fine to ~tens of
  thousands of blocks; rarity bucketing is the known upgrade).
- No cross-fetcher endgame coordination and no upload choking — org
  swarms are small and authenticated; incentives are the ledger's
  problem (§10), not the scheduler's.
