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

Every listener is bound to the box's own primary IP via `default_bind
{$AUTONOMY_BIND_IP}` — the ONE per-host value, supplied to caddy by a systemd
drop-in that `deploy-caddy.sh` installs (`--bind-ip`, defaulting to the
target's address). The config file itself is byte-identical on every estate
box. Explicit binding matters because the same machine also has a second
public IPv4 for coturn; a wildcard `0.0.0.0` listener would take that
address's TCP 80/443 and make TURN/TLS plus standalone certificate renewal
impossible.

**The base-url is not a routing concern.** The registry issues share links on
`https://relay.auto.network` via the service unit's `--base-url`. Adding the
apex vhost does not, and must not, touch that: a link minted after the apex
lands still points at `relay.auto.network`. Do not add a base-url override in
`estate.Caddyfile` or in the systemd unit.

## Files here

- `estate.Caddyfile` — the one complete authored configuration for all
  four current address blocks. This is the source of truth; clean hosts never
  pull configuration back from an old host.
- `../deploy-caddy.sh` — checksum, target-side validation, atomic install,
  rollback copy, and reload. It never captures the live file into the repo.

All four blocks deliberately have identical routing and no `encode` directive.
The read-only host inventory on 2026-08-13 found the live file uncompressed;
retaining that observed routing avoids bundling a compression change into the
recovery artifact. `deploy-caddy.sh` prints any later live divergence before
it replaces the file, then keeps the prior file for rollback.

## Ordering (do not invert)

Caddy mints the auto.network certificate over HTTP-01, which needs
`auto.network` to already resolve to this box. So:

1. `../add-apex-a-record.sh` on **auto-ash-1** — adds the apex A record.
2. Wait for propagation (checked across multiple resolvers).
3. `../deploy-caddy.sh` — installs the complete configuration; Caddy then obtains the cert.
4. `../verify-apex.sh` — proves DNS, cert, `/healthz`, and the `/install`
   content contract end to end, and that share links still use relay.

Deploying the vhost before DNS resolves makes cert issuance fail against a name
that does not yet point at the box. The DNS script and runbook preserve this
ordering; the Caddy deploy itself also supports pre-configuring a replacement
host before an intentional DNS cutover.
