# relaykit — auto.network relay tunnel + E2E channel crypto

The transport layer of auto.network share links (spec `graph://a17c8657-939`
§5.1–§5.2, invariant I5). Bead `auto-xbt33` (B2). Builds on idkit (A1) and
the registry service (B1); the registry-side relay endpoints live in
`tools/network/registry/relay.py` and are served by the same
`python -m tools.network.registry` process.

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
  all close `4404` — indistinguishable (anti-enumeration, §5.3).

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
3. `SERVER_HELLO` carries the dashboard's ephemeral key + a
   `tunnel:serve` cert chain + an Ed25519 signature over
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

`TunnelConnector(relay_url, org, key, cert, handler)` — `handler(token,
message) -> response` is where C4 plugs the target resolver. The connector
is deliberately a library + CLI (`python -m tools.network.relaykit.connector`)
rather than a wired dashboard background task: launching it requires org
key material that only exists after the C-track ceremonies (C2 session
keys / C5 agent delegation) land. Echo mode is the reference handler.

## Tests

```bash
pytest tools/network/relaykit/tests/
```

22 unit tests (framing, handshake pinning, record layer) + 7 integration
tests (two-process stack via the real `__main__` entrypoints: 1.55 MB soak,
ciphertext-wire tap, uniform close codes, SIGKILL-and-reconnect, MITM ×2 +
passthrough control).
