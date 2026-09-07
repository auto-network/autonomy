# Independent dictation service

The `voice-gateway` service owns browser voice sockets and transcript buffers.
It runs the shared voice protocol without a reload watcher. `whisper` owns one
shared model and separate audio/transcription state per client. Restarting the
dashboard worker leaves both processes alone.

## Enable

Set `VOICE_SIDECAR_ENABLED=true` and add `voice` to `COMPOSE_PROFILES` in the
deployment environment, preserving existing profiles, then run `docker compose up -d`.
Both `voice-gateway` and `whisper`
must be present. With voice disabled, set `VOICE_SIDECAR_ENABLED=false` and omit
the voice profile; the dashboard keeps its legacy voice endpoint.

The gateway publishes **only** host loopback `127.0.0.1:${VOICE_BACKEND_PORT:-8082}`
to container port 8082. Check for existing listeners before selecting the host
port; do not stop another session's service to free the default. It terminates TLS
with the node's existing `/app/data/tls.crt` and `tls.key`. An operator on the
host publishes the separate Tailnet TCP listener:

```sh
tailscale serve status --json
tailscale serve --bg --tcp=8443 tcp://127.0.0.1:8082
tailscale serve status --json
```

The commands above use the default backend port; substitute the configured
`VOICE_BACKEND_PORT` in the TCP destination when it differs.
Compare the before/after status: public 8443 must be a raw TCP forward to that port,
and the existing public 443 forward must be unchanged. Do not select HTTPS
proxy or TLS-terminated TCP mode: the gateway performs TLS termination.

Default browser Origin is HTTPS public 443; voice Host is public 8443 on the
same hostname. `VOICE_DASHBOARD_PUBLIC_PORT` accepts a single port or a
comma-separated allowlist, for example `443,8080`. Configure every intended
browser-facing dashboard port, not merely its proxy listener. Internal dashboard
8080 belongs in this comparison only when the browser actually accesses that port.
Check the browser's actual Origin before cutover. Reload the PWA document after
enabling the service so it discovers the new endpoint.

### Current host deployment (2026-09-07)

The host reported deployment with `VOICE_BACKEND_PORT=8444`, public voice 8443,
`COMPOSE_PROFILES=service-gateway,voice`, and
`VOICE_DASHBOARD_PUBLIC_PORT=443,8080`. Both HTTPS 443 and direct HTTPS 8080 are
operator-facing dashboard ports on `desktop-noft5ms.tail35c24e.ts.net`.
Tailnet 8443 forwards raw TCP to `127.0.0.1:8444`; existing 443 → 8080 is unchanged.
Host port 8082 remains occupied by another session's test dashboard.

The initial 443-only Origin configuration refused the operator's 8080 browser
with HTTP 403. Fix `e0e3dfc14d` added the explicit port allowlist and refusal
diagnostics. After configuration and gateway restart, the host observed an
accepted socket and a new Whisper client. This is connection evidence, not
completion of the live continuity exercises below.

The gateway verifies the existing signed, revocable human cookie and rejects
missing or foreign Origins. Buffer ownership uses the signed login identifier
and target session; separate logins cannot evict or restore each other's drafts.
This is login isolation, not a new organization-membership authorization system.

The gateway has no Docker or host tmux socket. It reads session lifecycle state
from the node data volume. That volume is currently writable because the reused
SQLite DAOs initialize schemas/connections and use WAL; code is mounted read-only.
Normal Send remains the browser's durable outbox. The compatibility WebSocket
commit command uses a token restricted to POST `/api/internal/voice-commit`.
Dashboard supervisor startup rotates that token; worker reload does not.

## Capacity and lifetime

`WHISPER_MAX_CLIENTS` defaults to 4. `WHISPER_MAX_CONNECTION_TIME` defaults to
86400 seconds; this replaces the implicit upstream lifetime cap. Both are passed
explicitly to WhisperLive. All clients share the configured model weights, but
have distinct upstream connections, audio windows, and transcript accumulators.
Concurrent connections do not imply simultaneous GPU execution or guaranteed
latency. Measure at one, two, and four active speakers before increasing the cap.
An upstream or gateway restart still requires voice recovery. Existing browser
draft preservation protects text already visible; audio not yet transcribed is
not queued for replay.

## Required live verification

Exercise authenticated voice connections from **both** configured dashboard
Origins (HTTPS 443 and HTTPS 8080 on the deployed hostname). Verify transcription,
not just `/health`: an unauthenticated handshake is expected to fail. Check that
unlisted ports, foreign hostnames, and missing/invalid login cookies are rejected.
Gateway logs distinguish Origin mismatch from missing/invalid login cookie;
inspect that reason when a browser reports a bare 403.

Keep a nonempty draft and an active microphone. Restart only the dashboard
worker 20 times, including a 90-second outage. Confirm the gateway and Whisper
PIDs and browser socket remain unchanged, speech continues to accumulate, and
the prior draft survives byte-for-byte. Send during the outage and confirm the
queued snapshot is delivered after recovery. Requests whose POST already began
remain unconfirmed after an ambiguous failure rather than being auto-replayed.
Repeat with two authenticated logins and different spoken text to check isolation.
The final acceptance exercise must use the standalone iPhone PWA and Bluetooth.
