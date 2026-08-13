# Dashboard WebRTC stack selection

Status: selected for the direct-path adapters. This decision does not change
the application handshake, authorization, relay-first ordering, or signaling
contract.

## Decision

Use standards-based WebRTC Data Channels (SCTP over DTLS over ICE) for every
direct dashboard path.

- Browser viewers use the browser's `RTCPeerConnection` and
  `RTCDataChannel` implementations.
- Python dashboards use `aiortc==1.15.0` for both browser-to-dashboard and
  dashboard-to-dashboard connections.
- `github.com/pion/webrtc/v4@v4.2.13` is an independent interoperability test
  fixture. It is not linked into the product and does not change the size or
  dependency set of the Go terminal client.
- The selected STUN/TURN deployment is coturn, operated as a separate service
  from the Python registry and relay.

There is no separate raw native ICE protocol. ICE nominates a packet path; it
does not supply the DTLS and SCTP layers a browser DataChannel requires. A raw
native path would therefore create a second application transport without
removing the full WebRTC implementation.

The existing root-pinned application handshake still runs over the selected
DataChannel. DTLS authenticates the ephemeral WebRTC connection; it does not
replace Autonomy's organization or link authorization.

## Interoperability evidence

The selection was exercised on 2026-08-13 with disposable programs outside
the repository. The fixtures used Chromium, released Python packages,
released Go modules, and an in-process Pion STUN/TURN server. No result below
is inferred from a feature list.

| Pair or path | Result |
| --- | --- |
| Chromium browser to aiortc | DataChannel opened and echoed `aiortc-echo:browser-ping`. |
| aiortc to aiortc | DataChannel opened and echoed `right:native-ping`. |
| Chromium browser to Pion fixture | DataChannel opened and echoed `pion-echo:browser-ping`. |
| aiortc to Pion fixture | DataChannel opened and echoed `pion-echo:native-ping`. |
| STUN-selected browser to Pion fixture | DataChannel passed; Chromium reported a selected `srflx`/`srflx` candidate pair. |
| TURN/UDP-only browser to Pion fixture | DataChannel passed; Chromium reported a selected `relay`/`relay` pair and emitted only a relay candidate. |
| TURN/TCP-only browser to Pion fixture | DataChannel passed over a selected `relay`/`relay` pair. |
| TURN/UDP-only aiortc to Pion fixture | aiortc gathered a relay candidate, the offer was restricted to that candidate, and data passed. |
| TURN/TCP-only aiortc to Pion fixture | aiortc gathered a relay candidate, the offer was restricted to that candidate, and data passed. |
| TURN/TLS-only aiortc to Pion fixture | aiortc connected through a locally trusted TLS certificate and data passed. |
| aiortc cancellation and cleanup | Ten concurrent aiortc-to-aiortc sessions exchanged data, both ends closed explicitly, and the retained responder and background-task counts returned to zero. |

The TURN/TLS result proves aiortc's `turns:` code path, not the production
certificate or public listener. Production acceptance must independently
force a dashboard connection through `turns:turn.auto.network:443`, verify the
public certificate normally, and exchange application data through coturn.

The host path also passed. Chromium offered a host candidate and exchanged the
same DataChannel payload directly. That single-machine fixture is not evidence
for symmetric NAT or IPv6 behavior; both remain explicit production and
upgrade-suite cases.

## Adapter contract

The signaling and application layers must not depend on aiortc objects. Each
adapter owns:

1. constructing a peer connection from the frozen ICE policy and issued
   STUN/TURN credentials;
2. applying the bounded offer/answer data from the signaling contract;
3. exposing one reliable, ordered byte-message channel to the existing
   application handshake;
4. reporting connection-state changes without logging SDP, candidates,
   addresses, credentials, or application bytes; and
5. closing the DataChannel and peer connection on local cancellation,
   timeout, signaling-socket close, or failed application handshake.

An abrupt remote disappearance is not treated as immediate cleanup: ICE
consent timeout may retain that peer temporarily. The implementation must cap
concurrent peers and close deterministically on every locally observable exit.

This boundary permits the WebRTC implementation to be upgraded without
changing the application wire protocol. Compatibility is determined by
WebRTC and the application handshake, not by matching library versions at the
two ends.

## Message-size rule: reuse the existing record protocol

aiortc advertises a 65,536-byte SCTP message limit. The current encrypted
channel normally emits records from plaintext chunks of up to 128 KiB, so
sending those records unchanged as individual DataChannel messages would
exceed the selected implementation's limit.

The direct adapter therefore uses the existing channel record chunking with a
60 KiB maximum plaintext chunk. The encrypted record adds 25 bytes, keeping
each DataChannel message safely below 65,536 bytes. The receiver already
accepts any record chunk up to 128 KiB, so this changes neither the record
format nor the application message limit. Large messages and attachments are
split and reassembled by the existing sequence-and-final-flag machinery.

Do not add a second fragmentation header, signaling operation, or protocol
version for WebRTC.

## One-server limitation on the Python end

aiortc currently passes only one STUN server and one TURN server to aioice and
silently ignores later entries. It also accepts only password credentials and
requires TCP for `turns:` URLs.

The configuration layer must not pass a list and imply failover. It selects
and validates exactly one STUN URL and one issued TURN URL for each aiortc
connection, rejecting ambiguous or unsupported input before constructing the
peer connection. This is acceptable while STUN/TURN is one co-located testing
center and the existing application relay remains the availability floor. It
means the Python endpoint has no ICE-server failover. Supporting multiple
independent TURN sites requires a deliberate adapter or library change, not
merely adding URLs to configuration.

Browsers may consume the fuller standards-compatible ICE server list their
implementation supports. That operational asymmetry is explicit; it is not a
wire difference.

## Packaging and runtime gates

`aiortc==1.15.0` requires `av>=14,<18`, plus aioice, pylibsrtp, cryptography,
and pyOpenSSL. The disposable environment resolved PyAV 17.1.0; the current
dashboard image carries PyAV 18.1.0, so installing aiortc is a dependency
downgrade, not an additive one. The complete transitive set must be pinned and
the existing dashboard tests rerun before the dependency enters the image.

PyAV's binary wheels bundle FFmpeg. The acceptable distribution/license shape
for that bundled build has not been established for this product. Do not ship
the PyPI binary wheel by assumption: packaging must either use an approved
FFmpeg build or obtain an explicit license disposition. This packaging gate
does not change the selected wire protocol.

aiortc's SCTP processing runs in Python on the dashboard event loop. The
adapter must apply DataChannel buffered-amount backpressure and bound peers;
its implementation acceptance includes measuring dashboard event-loop delay
during concurrent large transfers. Capacity is set from that measurement,
not from a claim that asynchronous APIs cannot consume the loop.

## Alternatives rejected

### One raw ICE implementation for native peers

Rejected because it invents a second transport. Browser interoperability
still requires WebRTC's DTLS/SCTP stack, so a raw path adds framing, security,
reliability, cancellation, and conformance work rather than replacing it.

### Pion as a product dependency or dashboard sidecar

Rejected for the current mission. Python dashboard-to-dashboard traffic is
already covered by aiortc at both ends, while Pion in the Go terminal client
would exceed that client's accepted binary-size target. A Pion sidecar would
add a process, local RPC protocol, supervision boundary, and failure mode.
Pion remains valuable as an independently implemented test peer.

### webrtc-rs

Rejected because Autonomy has no Rust runtime or build system in either
endpoint. Its release maturity is not used as a discriminator: aioice is also
pre-1.0, so applying that test only to Rust would be inconsistent.

### libdatachannel

Rejected for this implementation. It is browser-compatible and focused on
DataChannels, but brings a C/C++/FFI build plus TLS, usrsctp, and ICE backend
dependencies. That native build surface is unnecessary for the Python
dashboard path already exercised with aiortc.

### Google's native libwebrtc

Rejected because its media-oriented native build and release integration are
far larger than this DataChannel-only requirement. No exercised path required
embedding the browser's own implementation.

## Version and upgrade rule

The versions above are exercised implementation pins, not application wire
versions. Upgrade one WebRTC implementation at a time, capture its full
transitive dependency set, and repeat at least:

- browser-to-dashboard and dashboard-to-dashboard DataChannel exchange;
- exchange with the independent Pion fixture;
- forced direct, STUN, TURN/UDP, TURN/TCP, and production TURN/TLS paths;
- symmetric-NAT and IPv6 paths;
- messages spanning multiple 60 KiB encrypted records;
- event-loop delay and buffered-amount behavior under concurrent transfer;
  and
- repeated local cancellation with no retained peer connections or tasks.

Do not add a library-version field to signaling or the application handshake.
A new implementation version that remains WebRTC-compatible is forward-
compatible by the standard wire; one that fails this suite does not ship.

## Primary upstream references

- [aiortc project](https://github.com/aiortc/aiortc)
- [aiortc 1.15.0 package metadata](https://pypi.org/project/aiortc/)
- [aioice implementation](https://github.com/aiortc/aioice)
- [Pion WebRTC](https://github.com/pion/webrtc)
- [coturn](https://github.com/coturn/coturn)
- [PyAV](https://github.com/PyAV-Org/PyAV)
- [WebRTC Data Channels, RFC 8831](https://www.rfc-editor.org/rfc/rfc8831.html)
