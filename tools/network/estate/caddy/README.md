# estate Caddy — apex front door

The auto.network estate's TLS + routing front is Caddy on **registry-ash-1**
(`5.161.219.195`). It terminates HTTPS for the auto.network hostnames and
reverse-proxies each to the loopback services behind it.

## Hostname boundary (decided, bead auto-9q7a5)

| Host | Serves | Proxy target |
|---|---|---|
| `auto.network` | public front door + agent-first `/install` + `/healthz` | `127.0.0.1:8477` |
| `registry.auto.network` | registry / control API compatibility | `127.0.0.1:8477` |
| `relay.auto.network` | issued share links + viewer WebSockets | `127.0.0.1:8477` |

All three front the **same** registry uvicorn process on `127.0.0.1:8477`
(`tools/network/registry`, `autonomy-registry.service`). Splitting them is a
naming/contract boundary, not three processes.

**The base-url is not a routing concern.** The registry issues share links on
`https://relay.auto.network` via the service unit's `--base-url`. Adding the
apex vhost does not, and must not, touch that: a link minted after the apex
lands still points at `relay.auto.network`. Do not add a base-url override in
`auto.network.caddy` or in the systemd unit.

## Files here

- `auto.network.caddy` — the apex vhost fragment. The single reproducible
  source of the front-door route. Installed by `../deploy-apex-vhost.sh`.
- `registry-ash-1.Caddyfile.captured` — the **full** live Caddyfile as pulled
  back from the host by `deploy-apex-vhost.sh`. It is captured, not authored:
  the box's Caddyfile was previously host-only (uncaptured), so the VM was not
  clean-room reproducible. This file is that capture. Treat it as evidence of
  the live state, and reconcile the golden-snapshot build (auto-pqcsh) against
  it — do not hand-edit it as if it were the source of truth for the whole box.

## Ordering (do not invert)

Caddy mints the auto.network certificate over HTTP-01, which needs
`auto.network` to already resolve to this box. So:

1. `../add-apex-a-record.sh` on **auto-ash-1** — adds the apex A record.
2. Wait for propagation (checked across multiple resolvers).
3. `../deploy-apex-vhost.sh` — installs this vhost; Caddy then obtains the cert.
4. `../verify-apex.sh` — proves DNS, cert, `/healthz`, and the `/install`
   content contract end to end, and that share links still use relay.

Deploying the vhost before DNS resolves makes cert issuance fail against a name
that does not yet point at the box; `deploy-apex-vhost.sh` refuses to run until
the apex resolves from ≥2 public resolvers, which enforces this ordering.
