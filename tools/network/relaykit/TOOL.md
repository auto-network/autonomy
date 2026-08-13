# relaykit — auto.network relay tunnel + E2E channel crypto

The transport layer of auto.network share links (spec `graph://a17c8657-939`
§5.1–§5.2, invariant I5). Bead `auto-xbt33` (B2). Builds on idkit (A1) and
the registry service (B1); the registry-side relay endpoints live in
`tools/network/registry/relay.py` and are served by the same
`python -m tools.network.registry` process.

G1 (`auto-57hav`, spec `graph://eb245082-b76` §8) adds the network-fabric
rungs above that floor: the peer relay, the direct path, and the fallback
chain that walks them — see "G1: the connectivity fallback chain" below.

## Architecture

```
viewer/bootloader ──ws──▶ registry relay ◀──ws── dashboard connector
   (anonymous)             (untrusted:              (dials OUT,
                            routes ciphertext,       zero inbound ports,
                            token/org/timing         tunnel:serve hello,
                            metadata only)           reconnect/backoff)
        └───────────── X25519 + AES-256-GCM E2E ─────────────┘
```

- **`/t/{org}`** — one persistent outbound WS per org, dialed by the org's
  dashboard, authenticated by a `tunnel:serve`-scoped idkit hello verified
  against the org's bound root (same I4 discipline as registry mutations).
  A newly authenticated tunnel replaces the previous one (heals half-dead
  TCP).
- **`/v1/links/{token}/channel`** — where viewers connect. Token resolves
  with envelope-endpoint liveness rules; unknown/expired/revoked/offline
  all close `4404`; after the bootloader resolves a valid envelope, this is
  reported honestly as a disconnected sharing dashboard.

## Q1 decision — mux framing

Raw WS binary frames, 17-byte header, no mux library:
`[1B type][16B channel_id][payload]` with types OPEN (0x01, relay→dashboard,
payload `{"token": ...}`), DATA (0x02, opaque), CLOSE (0x03). WS is already
message-oriented — a stream-mux library would add dependency and semantics
we don't need. Channel ids are 16 CSPRNG bytes minted per viewer.

## Q3 decision — chunking / backpressure

Channel messages are chunked at 128 KiB before sealing; each chunk is one
AES-GCM record (`[8B seq][ciphertext]`, nonce = direction‖seq, AAD =
transcript‖direction‖seq, flags byte marks the final chunk). Senders await
every record send, so TCP backpressure propagates naturally; no tunnel
message ever approaches WS `max_size` limits. Verified by a 1.55 MB
byte-exact soak through the full two-process stack.

## I5 — how the relay is locked out

1. Viewer fetches the **envelope** over HTTPS first: org + `root_pub`.
   That root is the pin.
2. `CLIENT_HELLO` carries the viewer's ephemeral X25519 key.
3. `SERVER_HELLO` carries the dashboard's ephemeral key + an identity-neutral
   direct-root `tunnel:serve` cert + an Ed25519 signature over
   `(org, token, client_eph, server_eph)`.
4. The viewer verifies chain→pinned-root (scope `tunnel:serve`) then the
   signature. A relay substituting either ECDH key cannot re-sign; a relay
   substituting the cert cannot chain to the pin. Handshake fails closed —
   proven by an actively hostile relay implementation in the test suite
   (`tests/evil_relay.py`) with a passthrough control run.
5. Keys are HKDF-derived from the ECDH secret salted with the transcript
   hash (which includes the cert), so records can't be spliced across
   channels; strict seq ordering kills replay/reorder.

What the relay CAN see is the accepted §5.2 metadata set — token, org,
timing, volume — asserted in tests by a TCP tap on the tunnel wire: the
token appears in captured bytes (positive control), channel plaintext
never does.

## Dashboard integration seam

`TunnelConnector(relay_url, org, key, registry_cert, handler,
channel_cert=viewer_cert)` — `handler(token, message) -> response` is where C4
plugs the target resolver. `registry_cert` may carry the org-scoped persona
needed for admission; `viewer_cert` is identity-neutral and is the only one
placed in `SERVER_HELLO`. The connector
is deliberately a library + CLI (`python -m tools.network.relaykit.connector`)
rather than a wired dashboard background task: launching it requires org
key material that only exists after the C-track ceremonies (C2 session
keys / C5 agent delegation) land. Echo mode is the reference handler.

## G1: the connectivity fallback chain (spec §8)

How two org endpoints get connected: **direct → org peer relay →
auto.network floor.** The org absorbs load; the center provides the
floor, not the ceiling. Every rung carries the SAME E2E channel — the
transport is untrusted everywhere, so falling down the chain trades
latency and metadata exposure, never confidentiality or integrity.

```
dialer ──1 direct──────────────────────────▶ node (direct.py listener)
       ──2 peer relay (peer.py)──parked t──▶ node
       ──3 central floor (B2 relay)──tunnel▶ node
```

- **`dialer.dial_peer()`** walks the chain: races the target's candidate
  addresses from the registry's reachability hints (short per-attempt
  timeout), degrades to org peer relays, floors at the B2 viewer channel.
  A `HandshakeError` anywhere aborts the dial — a failed pin is an attack
  indicator, never "try elsewhere"; every other failure routes around.
- **`peer.PeerRelay`** — a member node's relay service. Nodes park B2-style
  serve-tunnels at `/t/{org}` (keyed by the node's public key: the key IS
  the address); dialers request bridges at `/dial/{org}`. The relay speaks
  the B2 mux and never parses past the frame header.
- **`relay:serve` is a delegation, not configuration.** The relay proves
  it may serve by signing the client's fresh nonce with a `relay:serve`-
  scoped chain to the org root (`peer.build_relay_hello` /
  `verify_relay_hello`; dial hellos also bind target + session against
  splicing). BOTH client roles verify before any channel byte flows: a
  dialer refuses an undelegated relay, and a would-be parker never
  presents its tunnel hello to one. The same grant is recorded in the org
  authority ledger as a `delegate` event carrying `relay:serve` — the
  ledger is discovery (`dialer.relay_candidates` = ledger live-keys ∩
  registry hints), the chain is on-wire enforcement, and revoking the
  ledger delegation drops the node from every fold's candidate set (L4).
- **`direct.py`** — rung one: the node's plain WS listener + the candidate
  client. "ICE-style" honestly: multiple address candidates from hints,
  first-success-wins over ordinary outbound TCP/WS. Real STUN/UDP
  hole-punching is a later additive transport (iroh/WebRTC territory);
  the chain's *shape* is what G1 pins.
- **`node.py`** — a member node's composite runtime behind ONE
  `handler(token, message)` seam: direct listener, floor tunnel, verified
  peer-relay parks, and the reachability announcer (hints are a lease —
  refreshed on a heartbeat, expiring when the node goes quiet).
- **Reachability hints** live in the registry
  (`POST /v1/orgs/{org}/reachability[/query]`, scopes `node:announce` /
  `node:lookup`): self-announced only (row keyed by the envelope signer),
  TTL-expired, and Tier B both ways — anonymous sessions can never
  enumerate an org's interior addresses.

v1 boundaries, deliberate: the peer relay holds no revocation denylist
(a revoked-but-unexpired tunnel cert is caught at the registry floor and
by cert renewal); the floor rung addresses the org's B2 tunnel by link
token (per-node floor addressing arrives when the floor learns node
keys); transports are TCP/WS only.

Bulk transfer over these links — content-addressed blocks, have/want
maps, rarest-first — is G2, `tools/network/swarmkit/` (rides the same
channel contract via `swarm_handler`).

## Tests

```bash
pytest tools/network/relaykit/tests/
```

22 unit tests (framing, handshake pinning, record layer) + 7 integration
tests (two-process stack via the real `__main__` entrypoints: 1.55 MB soak,
ciphertext-wire tap, uniform close codes, SIGKILL-and-reconnect, MITM ×2 +
passthrough control).

G1 adds 16 in-process tests (`test_peer.py`: relay-hello freshness/binding/
scope refusal, park+dial bridge, dialer AND parker refusing an undelegated
relay, relay-process frame scan seeing ciphertext only, MITM by an
*authorized* relay failing closed, ledger∩hints discovery incl. revocation
cascade) + 5 three-process acceptance tests (`test_fallback_integration.py`:
registry + peer-relay + node subprocesses; direct control, simulated-NAT
fall to peer relay, SIGKILL fall to floor, one payload digest across all
three paths).
