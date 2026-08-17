# auto.network coturn deployment

This deploys the official coturn container on the same Hetzner server as the
registry and application relay, but on a second public IPv4. The address split
is load-bearing: Caddy owns TCP 80/443 on the original address; coturn owns
UDP/TCP 3478 and TCP 443 on the TURN address. Prometheus is reachable only at
`127.0.0.1:9641` on the host.

The pinned linux/amd64 image is coturn 4.17.2-r0 at
`sha256:75e9ebd1e19005bec0c7f591d29afe22f959916ac8d9c852452f27db8c789828`.
The renderer refuses Caddy's `5.161.219.195` address, so a typo cannot place
coturn on the existing HTTPS listener.

## Secrets and certificates

`turn-rest-secrets` contains one 64-character lowercase hexadecimal secret,
or two during rotation. It is an HMAC credential for coturn's TURN REST
authentication, not an organization identity or application authorization
key. It is never placed in an environment variable, command line, repository,
metric label, or persistent generated config. Systemd copies it into its
credential directory and the renderer creates a group-readable config under
`/run`; the container runs with uid `nobody` and a dedicated host group that
can traverse and read only this runtime directory. The group id is measured by
the deploy and pinned in `runtime.env`, never assumed to be the host's
`nogroup` id.

The co-located Registry is the sole credential issuer. Its systemd unit reads
the same root-only file through a separate `LoadCredential` mount; Dashboards
and browsers never receive the long-lived secret. With two lines present,
coturn accepts both while the issuer signs new 15-minute coupons with the last
line. Rotation is therefore ordered: append the new line, restart coturn and
the Registry, prove credentials signed by both secrets, wait at least the
credential TTL, remove the old first line, then restart and prove again. A
Registry restart reconnects serving tunnels and must be included in the
monitored rotation window.

Once this optional feature is activated, the root-only secret file is an
installed-service invariant for both Coturn and the Registry. Removing or
corrupting it is an operator configuration failure, not a runtime failover
case; restore the file or remove the Registry drop-in before restarting. The
base Registry unit remains independent before TURN activation.

The authenticated org tunnel deliberately carries renewable TURN-minting power.
This adds no application authority: the same tunnel can already create public
links, and public links are TURN-eligible. Honest Dashboard callers still
enforce a live link grant or the local `turn:allocate` execution scope before
asking, but the Registry does not prove or attribute that local decision.

Coturn may include the opaque, expiring TURN username in its bounded session
logs. The username contains no org, persona, link, bearer, or source and cannot
authenticate without its password. Passwords and the long-lived REST secret
never enter logs, metrics labels, environment variables, or argv.

The TLS certificate and key come from Certbot's
`/etc/letsencrypt/live/turn.auto.network/` paths. Systemd handles them as
credentials and the renderer copies them into the same runtime directory.
Certificate renewal uses standalone HTTP-01 bound to the TURN address only;
Caddy must already be bound to the original address rather than `0.0.0.0`.
The renewal hook restarts coturn because upstream documents configuration
changes as restart-required.

## Ordered deployment

1. Attach the approved second public IPv4 to `registry-ash-1`.
2. Bind every Caddy site to the original IPv4 and deploy that file. Verify the
   existing registry, relay, install page, and certificates before continuing.
3. Add `stun.auto.network` and `turn.auto.network` A records for the TURN IP.
4. Add inbound cloud-firewall rules for TCP 80, TCP 443, TCP/UDP 3478, and
   the exact UDP relay range. The Hetzner cloud firewall is the enforcement
   point because Docker's destination NAT bypasses ordinary host INPUT rules.
   TCP 80 exists only for standalone ACME and TCP 443 is TURN/TLS; nothing
   else is opened for this service.
5. Run `deploy.sh root@registry-ash-1` without `--activate`. This installs the
   container runtime and artifacts but starts nothing.
6. Run `provision-secret.sh` once on the host. Never rerun it to repair a
   failure; rotation adds a second line, proves both, switches the issuer,
   waits the credential TTL, drains, and then removes the old line.
7. Copy `runtime.env.example` to `runtime.env` and fill the limits. The relay
   range is bounded to 100 ports for the first capacity run, but allocation and
   bandwidth values are intentionally blank until measured.
8. Set `TURN_PUBLIC_IP` and `ACME_EMAIL`, then run `issue-certificate.sh`.
9. Run `deploy.sh root@registry-ash-1 --activate`.
10. Prove metrics are private, inspect logs using a synthetic username, and run
    the external UDP/TCP/TLS, forbidden-destination, expiry, capacity, restart,
    and rollback matrix before advertising the URLs to product clients.

The first operating cap is at most half the first measured saturation point.
The relay port range is a hard resource bound, not a capacity claim. The
application relay remains the first-byte and failure fallback throughout.

The two-secret overlap was exercised against the pinned 4.17.2 binary: a
client authenticated and relayed data first with the old secret and then with
the new one while both lines were loaded. Removing the old line remains a
drain-and-restart operation; it is not assumed to hot-reload.

IPv6 is absent from the first deployment: coturn binds its listener and relay
pool only to the host's floating IPv4, and defaults allocations to IPv4. The
container uses host networking so the relayed source address is exactly that
floating IP; Docker bridge NAT is deliberately absent from the data path.
The process remains uid 65534. The read-only pinned image's turnserver binary
has only `cap_net_bind_service=ep`; Docker drops every capability and restores
only `NET_BIND_SERVICE` to the bounding set. `no-new-privileges` is
intentionally incompatible with that file capability and is absent, allowing
the non-root process to bind TURN/TLS 443 without granting root or any other
capability. The live listener check proves the effective result after deploy.
Enabling IPv6 is a separate change gated on completing the
IPv6 destination policy (including NAT64 `64:ff9b::/96`) and rerunning the
private, link-local, metadata, mapped-address, and multicast refusal matrix.
