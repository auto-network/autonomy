# serve.auto.network public edge (v1 — serve on its own IP, no caddy-l4)

serve.auto.network rides its **own** public IP (Hetzner floating IP
`serve-ash-1` = 5.161.17.217 on registry-ash-1). The raw-stream ingress
(auto-9z1xh) inside the registry binds that floating IP's `:443` directly and
is itself the public serve edge: it peeks the ClientHello SNI and routes, so no
SNI logic and no TLS termination live at the edge, and no separate forward
process sits in front of it. Because the ingress owns the public socket, the
socket peer **is** the real client, so per-source accounting sees the true
client address natively — there is no PROXY header and no loopback hop. The
primary IP's single Caddy (auto/registry/relay) is untouched. This deliberately
replaces the stacked caddy-l4 `:443` demux (auto-ot0t7) for v1; caddy-l4 remains
the path only if serve is ever consolidated back onto the shared IP or moved to
anycast.

The relay never sees plaintext (raw TLS passthrough → tunnel → the operator's
local Caddy terminates with the persona wildcard cert).

## Pieces (all on registry-ash-1)
- The registry's raw-stream ingress (`tools/network/registry`, unit
  `autonomy-registry.service`) binds `${SERVE_BIND_IP}:443`. It needs
  `CAP_NET_BIND_SERVICE` (granted in the unit) to bind the privileged port under
  `DynamicUser`, and it orders `After=`/`Requires=` the serve-ip unit below.
- `bind-serve-ip.sh` + `autonomy-serve-ip.service` — persist the floating IP
  on the NIC at boot (Hetzner floating IPs aren't auto-configured); the
  registry Requires it.
- DNS: the responder answers serve names → the floating IP via the DNS unit's
  `--relay-ip` (`DNS_RELAY_IP` in `/etc/autonomy-dns/dns.env`), set by
  `estate/dns/deploy.sh --relay-ip`.
- `/etc/autonomy-serve/serve.env`: `SERVE_BIND_IP`, `SERVE_INTERFACE`.

## Deploy / rollback
Floating IP: `hcloud floating-ip create --type ipv4 --home-location ash
--server registry-ash-1 --name serve-ash-1`. Then install the serve-ip unit and
deploy the registry (`tools/network/registry/deploy/deploy.sh`) so it binds the
floating IP's `:443`.
Rollback: point the registry's `--stream-ingress-host`/`--stream-ingress-port`
back at loopback (`127.0.0.1:8479`), `systemctl disable --now
autonomy-serve-ip`, set `DNS_RELAY_IP` back to the primary IP + restart
`autonomy-registry-dns`, and (optionally) `hcloud floating-ip delete
serve-ash-1`. No change to the primary `:443` at any point.

## Remaining for an external-browser demo
1. Persona wildcard cert via DNS-01 (auto-rvq3j) installed in the operator's
   local Caddy — the challenge TXT path is live (`dns_challenges` / bhs3c).
2. A dashboard connector serving a reservation bound to the target session:port
   (e.g. auto-0829-125521:3000), so the ingress has a live lease to route to.
