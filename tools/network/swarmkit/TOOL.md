# swarmkit — content-addressed swarm bulk fetch (G2)

Spec `graph://eb245082-b76` §8; fountain revision per the RaptorQ
decision note `graph://454424ca-10e` (bead `auto-0m2kp`, supersedes the
block scheduler of `auto-25dz3`). Builds on relaykit (G1): large
artifacts move over the same E2E channels the fallback chain
establishes — direct, peer relay, or central floor; the application
seam cannot tell which.

## Two transfers, one seam

| | fountain (primary) | blocks (superseded, retained) |
|---|---|---|
| unit | RaptorQ encoded symbol (RFC 6330) | fixed-size hashed block |
| scheduling | none — any symbol is useful | rarest-first + want-lists |
| publisher ≈1× egress | structural (cursor arithmetic) | latency accident (failed review at 3× on loopback) |
| integrity | decoded-object hash, fail closed | per-block hash |
| polluter handling | leave-one-out identification | per-block strike-out |
| modules | `fountain*.py` | `store.py`/`protocol.py`/`fetch.py` |

The block path stays for now because its store/manifest/handler shapes
are load-bearing elsewhere and its per-block-verifiable manifests still
fit small hot artifacts; new bulk-transfer callers use the fountain
path. Both compose behind one handler chain:

    fountain_handler(fstore, fallback=swarm_handler(bstore, fallback=app))

## Fountain transfer (RaptorQ, RFC 6330)

The codec is the vetted `raptorq` PyPI package — PyO3 bindings of the
Apache-2.0 [cberner/raptorq](https://github.com/cberner/raptorq) crate
(pinned in `deploy/requirements.txt`; attribution note there). The
codec is **never** reimplemented here.

An object is committed by a tiny manifest `{size, symbol_size,
object-sha256}`; the artifact id is the SHA-256 of the canonical-JSON
manifest. Any peer emits interchangeable encoded symbols; a leecher
collects any K+ε from anyone, decodes, and verifies the object hash —
integrity is end-to-end, the transport stays untrusted.

### Why publisher egress ≈1× is structural now

- A complete seeder serves **fresh symbols through a monotonic
  per-artifact cursor** — it never re-serves a symbol, so its egress
  equals the count of *distinct* symbols it contributed. Latency
  cannot inflate it. Concurrent leechers receive complementary slices
  and complete by **trading** (partial holders serve their stored
  packets round-robin).
- Requests carry the leecher's holdings as packed `(SBN,ESI)` id
  ranges (`exclude`), so nothing already held crosses the wire.
- **Stripes** make multiple complete seeders complementary: the
  bindings only generate the deterministic packet stream as a prefix,
  so seeder `i` owns interleaved stream positions `p ≡ stripe_i (mod
  n_stripes)` (cost O(served × n_stripes) at ~800 MB/s marginal
  encode, not O(stripe base)). Stripes derive from the org roster's
  stable member ordering (`roster_stripe`) — no runtime coordination.
  Past `n_stripes` seeders, stripes recycle: duplicate waste, never
  wrong bytes.

Measured on zero-latency in-process links (the case the block
scheduler failed at 3.0×): publisher egress **1.023×**, zero duplicate
serves, ~⅔ of every leecher's symbols traded from fellow leechers.

### Pollution (the one real fountain tradeoff)

Coded symbols don't self-verify against a fixed manifest — a member
can serve wrong-linear-combination symbols that corrupt the whole
decode. Handling: verify the **decoded object** against its hash and
fail closed; then leave-one-out over contributing members (re-decode
from everyone-but-one, topping up from surviving links) — a
hash-verified decode names the polluter and completes the fetch
honestly; no single-exclusion success = stay failed closed. Peers are
authenticated org members, so the named member is revocable via the
ledger. Attribution is to the *serving* member (a member vouches for
what it serves); publisher-signed per-symbol commitments are the v2
hardening that would also localise poison relayed through honest
members.

### Wire ops (over the `handler(token, message)` seam)

    {"v":1, "op":"fountain.manifest", "artifact": <64 hex>}
    {"v":1, "op":"fountain.symbols",  "artifact": ..., "count": 1..64,
     "exclude": [[start,end], ...]?}

Symbol responses are a canonical-JSON header line + `n` serialized
packets (4-byte payload id + one symbol each). `n=0` means "nothing
new for you right now" — idle, not an error. `fountain_handler.metrics`
(`FountainMetrics`) meters egress in bytes, packets, *and distinct
symbol ids* — `served_packets == len(served_ids)` is the never-re-served
invariant the acceptance pins.

### Fetch (`fountain_fetch`)

One session per link; each loops "up to *n* symbols I don't hold",
feeds an incremental decoder, and completion is **decoder-driven**
(no fixed K+ε count — RaptorQ usually finishes at exactly K,
occasionally a couple more). Sessions yield to the event loop each
round, so zero-latency links schedule fairly (this is what makes the
loopback egress test honest). Links are ephemeral: closed on every
path — success, timeout, pollution, error.

## Discovery — the roster is the tracker (no DHT)

Unchanged from the block design: the ledger's member/live-keys
projection says who exists, the registry's reachability hints say
where to dial, `dial_links()` establishes G1 channels (direct → peer
relay → floor). The same roster ordering hands every member its
stripe.

## Tests

```bash
pytest tools/network/swarmkit/tests/
```

Fountain: 35 unit/handler/loopback tests (manifest + range codecs,
stripe disjointness incl. past-N recycling, monotonic cursor, seam
composition, zero-latency 3-leecher egress ≈1×, multi-seeder
complementarity, churn, polluter identification, fail-closed single
source, link hygiene) + 3 network acceptance tests over real relaykit
stacks (`test_fountain_integration.py`):

- **50 MB, 3 leechers**: all decode + verify; publisher
  `served_packets == distinct` and distinct < 1.35×K; links idle out.
- **Multi-seeder**: two roster-striped seeders, two leechers —
  served sets provably disjoint, aggregate fresh symbols ≈ one object.
- **Polluting member**: poisoned payloads on the wire → decode-verify
  catches, leave-one-out names the member, fetch completes honestly.

Block-path tests (36 unit + 3 network) remain and pass.

## v1 boundaries, deliberate

- Stores are in-memory; pool extension transiently materialises the
  stream prefix (fine at tens of MB; disk-backed store + windowed
  generation slot behind the same surface later).
- Symbols are not individually signed — pollution through an honest
  relay attributes to the relay (which vouches by serving); signed
  symbol commitments are the hardening path.
- `n_stripes` defaults to 8; raise roster-wide when swarms outgrow it
  (stripe recycling degrades to bandwidth waste, never corruption).
- Publisher-vs-peer intake balance rides asyncio fairness per round;
  a shortfall-biased scheduler (poll peers first, publisher for the
  remainder) is the lever if real-world topologies skew it.
