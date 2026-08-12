# Relay size and performance — measured baseline

Measured 2026-08-12 on CPython 3.12.3, Linux x86-64, Ryzen 9 7950X3D.
These are local object/hot-path measurements, not a production capacity claim:
they exclude TLS, the real Starlette/WebSocket objects, kernel socket buffers,
NIC work, and cross-relay forwarding. A fresh process that imports and creates
the whole registry application was about 54 MB RSS before serving sockets.

These costs describe the guaranteed relay fallback. The preferred architecture
uses auto.network for a tiny cached bootloader and rendezvous, then sends the
data plane directly between viewer and dashboard whenever ICE discovers an open
path. Successful direct sessions do not consume relay viewer queues or relay
payload CPU at all.

## Scaling shape

For `T` tunnels, `V` viewers, and `S` active link streams:

- idle tunnel bookkeeping is `O(T)` memory and effectively zero application
  CPU while its WebSocket receive is asleep;
- viewer routing is `O(1)` per ordinary frame through a channel-id dict;
- each viewer has one bounded queue and one persistent writer task, so idle
  viewer memory/tasks are `O(V)`;
- each active stream has one retained deque and one listener per viewer;
- publishing one feed frame is necessarily `O(audience)` because one socket
  send must eventually happen per listener;
- pool admission is `O(tunnels for that org)` only when a viewer opens. It is
  not on the frame path.

The intended pool therefore needs no heap, scheduler service, or background
balancer. Scan the org's usually-small tunnel set, choose a member with the
fewest active channels and spare capacity, then pin the viewer.

## Idle memory

The isolated benchmark used real `Tunnel`, `_ViewerRelayChannel`, `Stream`,
`Listener`, `asyncio.Queue`, and suspended writer-task objects with a minimal
fake WebSocket.

| Shape | Python heap delta | RSS delta |
|---|---:|---:|
| 64 idle tunnels | 48.5 KB | 36.9 KB (page-granularity noise) |
| 1 tunnel, 128 viewers, one shared stream | 678 KB | 1.13 MB |
| 1 tunnel, 128 viewers, unique streams | 824 KB | 1.31 MB |
| 8 tunnels, 1,024 viewers, shared per-tunnel stream | 5.34 MB | 9.31 MB |
| 8 tunnels, 1,024 viewers, unique streams | 6.48 MB | 11.08 MB |

The stable per-viewer application-object result is about 5.1 KB heap / 8.9 KB
RSS with shared streams and 6.2 KB heap / 10.6 KB RSS with unique streams.
Real WebSocket, ASGI, and kernel costs must be added by an end-to-end load test.

At the current 128-viewer-per-tunnel cap, the measured application objects
alone imply roughly:

| Viewers | Required tunnels | Shared-stream heap | Unique-stream heap |
|---:|---:|---:|---:|
| 10,000 | 79 | ~51 MB | ~62 MB |
| 100,000 | 782 | ~510 MB | ~620 MB |

Those extrapolations describe Python objects, not safe single-process targets.

## Buffered memory is the real bound

`VIEWER_QUEUE_MAX_BYTES` is 2 MiB and
`MAX_VIEWER_CHANNELS_PER_TUNNEL` is 128. Unique slow-channel payloads can
therefore account for 256 MiB per full tunnel. Materializing a 2 MiB unique
backlog for 16 viewers added 35.8 MB RSS in the benchmark, about 2.24 MB per
viewer including objects; a linear 128-viewer case is roughly 286 MB.

Shared feed frames are immutable `bytes` referenced by every listener queue,
so Python does not duplicate the payload per viewer. A 2 MiB shared backlog
accounted against all 128 viewers added only 3.24 MB RSS. The byte accounting
is intentionally conservative even when the allocation is shared.

Two limits are not hard process-memory bounds:

1. Stream retention has a 1 MiB target but preserves at least 10 frames. With
   today's approximately 128 KiB records that is about 1.25 MiB per lagging
   stream; with a larger admitted WebSocket message the floor overrides the
   byte target by much more.
2. The registry does not override Uvicorn's WebSocket defaults in `__main__`.
   In this environment those defaults are a 16 MiB maximum message and a
   32-message protocol queue. Lower-layer buffering can therefore exceed the
   relay's application queue before `_ViewerRelayChannel` accounts for it.

The first production performance work should make message size and all queue
depths jointly bounded. A language rewrite does not fix an unbounded or
misaligned buffering policy.

## CPU and event-loop turns

Median local synchronous hot-path timings (seven repeats per sample):

| Operation | Time | Approx. cycles at 4.2 GHz |
|---|---:|---:|
| encode 64-byte tunnel frame | 0.14 us | ~600 |
| decode 64-byte tunnel frame | 0.66 us | ~2,800 |
| channel dict lookup + enqueue call | 0.08 us | ~330 |
| encode one 128 KiB record | 2.23 us | ~9,400 |
| decode one 128 KiB record | 3.07 us | ~12,900 |
| least-load scan, 8 tunnels | 0.29 us | ~1,200 |
| least-load scan, 64 tunnels | 0.98 us | ~4,100 |
| least-load scan, 256 tunnels | 3.4 us | ~14,300 |
| stream publish bookkeeping, 1 listener | 0.91 us | ~3,800 |
| stream publish bookkeeping, 16 listeners | 2.26 us | ~9,500 |
| stream publish bookkeeping, 128 listeners | 11.7 us | ~49,000 |

The cycle conversion is illustrative because CPU frequency is dynamic. These
numbers omit WebSocket parsing, TLS, syscalls, and actual sends, which will
dominate the sub-microsecond dictionary work.

In application task activations rather than CPU cycles:

- viewer to dashboard: the viewer endpoint wakes, decodes, takes the tunnel
  send lock, and awaits one tunnel send;
- dashboard to viewer: the tunnel endpoint wakes and enqueues, then the pinned
  viewer's writer task wakes and performs one send;
- feed to `N` viewers: one tunnel/publisher task plus `N` queue handoffs and up
  to `N` writer-task wakeups.

Pooling adds a scan to channel open only. It adds zero steady-state frame hops
when tunnel and viewer share a relay process; a cross-relay anycast assignment
adds one internal network hop by definition.

## Source size and a native rewrite

Current physical size:

| Component | Total lines | Nonblank, non-comment lines |
|---|---:|---:|
| registry relay (`relay.py`) | 750 | 589 |
| tunnel/viewer framing (`frames.py`) | 133 | 83 |
| tunnel hello (`hello.py`) | 80 | 64 |
| dashboard connector (`connector.py`) | 520 | 426 |

The relay switch itself is a hundreds-of-lines program. A native implementation
of just WebSocket termination, authenticated tunnel-pool membership, fixed
framing, pinned admission, bounded queues, and forwarding should remain in the
hundreds of implementation lines with a mature async/TLS/WebSocket stack.

Full parity is a different boundary: certificate verification, link/token
storage, control operations, retained feed streams, exact close semantics,
deployment/metrics, and tests are already spread across more than the switch.
Rewriting all of that honestly is likely low thousands including tests even if
the forwarding core stays below 1,000 lines.

Rust is the lower-risk native choice for this boundary because its async,
TLS/WebSocket, and future QUIC ecosystem is substantially more complete. Zig
can produce a smaller binary and explicit allocator control, but today would
make more of the protocol stack our maintenance burden. Neither rewrite is
needed for the tunnel-pool fix; do it only when measured socket/TLS/GC overhead,
not the 3-microsecond admission scan, is the actual limiter.
