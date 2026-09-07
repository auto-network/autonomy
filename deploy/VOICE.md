# Independent dictation service

The `voice-gateway` service owns browser voice sockets and transcript buffers.
It runs the shared voice protocol without a reload watcher. `whisper` owns one
shared model and separate audio/transcription state per client. Restarting the
dashboard worker leaves both processes alone.

## Enable

Set `VOICE_SIDECAR_ENABLED=true` and `COMPOSE_PROFILES=voice` in the deployment
environment, then run `docker compose up -d`. Both `voice-gateway` and `whisper`
must be present. With voice disabled, set `VOICE_SIDECAR_ENABLED=false` and omit
the voice profile; the dashboard keeps its legacy voice endpoint.

The gateway publishes **only** host loopback `127.0.0.1:8082`. It terminates TLS
with the node's existing `/app/data/tls.crt` and `tls.key`. An operator on the
host publishes the separate Tailnet TCP listener:

```sh
tailscale serve status --json
tailscale serve --bg --tcp=8443 tcp://127.0.0.1:8082
tailscale serve status --json
```

Compare the before/after status: public 8443 must be a raw TCP forward to 8082,
and the existing public 443 forward must be unchanged. Do not select HTTPS
proxy or TLS-terminated TCP mode: the gateway performs TLS termination.

Default browser Origin is HTTPS public 443; voice Host is public 8443 on the
same hostname. Set `VOICE_DASHBOARD_PUBLIC_PORT` if the deployment uses another
public dashboard port. Internal dashboard 8080 never belongs in this comparison
unless the browser actually accesses that port. Reload the PWA document after
enabling the service so it discovers the new endpoint.

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

Keep a nonempty draft and an active microphone. Restart only the dashboard
worker 20 times, including a 90-second outage. Confirm the gateway and Whisper
PIDs and browser socket remain unchanged, speech continues to accumulate, and
the prior draft survives byte-for-byte. Send during the outage and confirm the
queued snapshot is delivered after recovery. Requests whose POST already began
remain unconfirmed after an ambiguous failure rather than being auto-replayed.
Repeat with two authenticated logins and different spoken text to check isolation.
The final acceptance exercise must use the standalone iPhone PWA and Bluetooth.
