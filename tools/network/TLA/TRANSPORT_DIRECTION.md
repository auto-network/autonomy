# Relay transport direction — anycast, RaptorQ, and NAT traversal

This is a boundary note, not a proposed implementation. It records what each
technology solves so the next design does not make one layer impersonate
another.

## North star: direct data, relay floor

In the preferred path, auto.network serves a tiny immutable/cacheable
bootloader plus the minimum rendezvous/signaling needed to introduce the two
endpoints. The viewer and dashboard then establish a direct encrypted channel,
and artifact bytes, requests, Q&A, presence, and live updates stop traversing
the relay.

```text
viewer -> anycast/cache: bootloader + rendezvous
viewer <---------------> dashboard: preferred direct data channel
viewer -> relay pool -> dashboard: zero-setup fallback when direct fails
```

Only the versioned bootloader is public cache material. Bearer link tokens,
sealed grants, candidate exchange, and presence/session metadata remain
link-specific and uncacheable; putting those in the cached object would turn a
performance optimization into credential leakage.

The relay is still essential. It is the path that works behind hostile NAT,
enterprise filtering, carrier networks, and routers nobody is willing or able
to configure. Direct discovery must be opportunistic and invisible: race or
trickle candidates, use the best validated direct pair, and retain the relay as
the dependable floor. A failed direct attempt must cost latency, not usability.

For a browser viewer, WebRTC Data Channels are the standards-based direct
candidate today: SCTP over DTLS over ICE/UDP supplies NAT traversal, encryption,
multiplexed streams, and reliable or partially reliable messages. WebTransport
is valuable when the dashboard already has a publicly reachable HTTP/3 server,
but it does not by itself give a browser peer-to-peer ICE path through a home
router.

The existing root-pinned application channel should remain above either
transport. WebRTC/DTLS protects the hop; the channel handshake keeps the same
org identity and end-to-end semantics on direct, peer-relay, and auto.network
fallback paths. Content code must not know which path won.

Primary browser data-channel reference:

- WebRTC Data Channels: https://www.rfc-editor.org/rfc/rfc8831.html

The exercised dashboard WebRTC stack selection is recorded in
`tools/network/ICE_STACK_SELECTION.md`.

## Anycast relay pool

`relay.auto.network` can be an anycast ingress while one org maintains several
outbound connector sockets terminating on several relay servers. The logical
pool is:

```text
org -> {tunnel endpoint, relay node, active viewers, capacity, lease}
```

An ingress edge selects a healthy least-loaded tunnel once. If that tunnel is
local, it forwards directly. If remote, it sends the viewer channel over one
internal relay-to-relay hop. The viewer remains pinned until that tunnel or
connection fails.

Anycast only chooses the first edge. It does not distribute connector sockets
reliably, discover remote tunnel state, preserve state when routing changes, or
select the newest connector. Those are separate directory, health/lease, and
internal-routing responsibilities. The current TLA+ model assumes the logical
directory/handoff works; a later distributed model must cover partitions,
stale leases, and node failure before this is deployed globally.

Do not add per-frame global coordination. Membership/lease changes update the
directory; viewer admission reads it once; bytes then follow the pinned route.

## Raptor and RaptorQ fountain codes

RaptorQ (RFC 6330) divides an object into source blocks and symbols. It is
systematic—the original source symbols can be sent—and can generate arbitrary
repair symbols. A receiver reconstructs a block from almost any sufficiently
large set of symbols. Each standard FEC payload ID contains an 8-bit Source
Block Number and 24-bit Encoding Symbol ID.

That is useful for erasure recovery and one-to-many object delivery. It is not
a multiplexer, flow scheduler, congestion controller, or NAT traversal
protocol.

### Do not put it on today's WebSocket path

The current relay uses reliable, ordered WebSocket/TCP. TCP already retransmits
loss and enforces ordering. Adding RaptorQ above it would add bytes, encoding,
decoding, and buffering while remaining stuck behind TCP head-of-line blocking.

### Where it can become useful

RaptorQ becomes relevant if a future transport intentionally carries bulk or
live data over unreliable datagrams, multiple independent paths, or a shared
fan-out where receivers experience different losses. QUIC DATAGRAM (RFC 9221)
is a natural possible substrate: reliable QUIC streams carry authentication,
control, object metadata, and exact operations; unreliable DATAGRAM frames
carry source/repair symbols without transport retransmission.

Multiplexing still needs an application identifier. RFC 6330's SBN/ESI names a
symbol within one FEC object; it does not identify the mission, channel, or
object among concurrent transfers. A plausible datagram boundary is:

```text
[flow id][object id][source-block number][encoding-symbol id][symbol]
```

Object length, symbol size, block partition, and encryption parameters can
travel once on a reliable control stream. This matches RFC 9221's rule that an
application defines identifiers for multiple datagram flows.

Use systematic symbols first and add repair symbols adaptively from observed
loss. Keep symbol size below the validated path MTU. Do not apply FEC to small
interactive control messages or reliable streams. Endpoints—not the opaque
relay—should encode/decode, and decoder CPU/working memory must be benchmarked
before choosing block sizes.

Primary references:

- RaptorQ: https://datatracker.ietf.org/doc/html/rfc6330
- original Raptor code: https://datatracker.ietf.org/doc/html/rfc5053
- QUIC DATAGRAM and application multiplexing: https://datatracker.ietf.org/doc/html/rfc9221

## ICE, STUN, and TURN

The clean discovery stack is:

1. gather local candidates;
2. use STUN (RFC 8489) to learn server-reflexive addresses and check
   connectivity;
3. allocate TURN (RFC 8656) relayed candidates as the dependable fallback;
4. use ICE (RFC 8445) connectivity checks to nominate the best working pair;
5. trickle candidates (RFC 8838) so checks start before gathering finishes;
6. retain a relay candidate and perform an ICE restart when the network
   changes.

STUN explicitly is not a traversal solution by itself. TURN supplies a public
relay allocation when direct paths fail. ICE is the selection procedure that
combines host, server-reflexive, and relayed candidates.

The current auto.network reverse tunnel should remain boot/rendezvous signaling
and the application-level zero-setup fallback, but it is not thereby a TURN server. A proper
TURN-compatible service also needs allocations, permissions/channel bindings,
authentication, lifetime refresh, abuse controls, and UDP/TCP/TLS transport
behavior.

Anycast needs special care for stateful TURN/ICE traffic: an allocation and its
5-tuple must keep reaching the node that owns it, or the allocation state must
be routed internally. Connection affinity or connection-ID-aware routing can
help, but the anycast address itself does not provide session continuity.

Primary references:

- ICE: https://www.rfc-editor.org/rfc/rfc8445.html
- STUN: https://www.rfc-editor.org/rfc/rfc8489.html
- TURN: https://www.rfc-editor.org/rfc/rfc8656.html
- Trickle ICE: https://www.rfc-editor.org/rfc/rfc8838.html
