# serve.auto.network public edge (v1 — serve on its own IP, no caddy-l4)

serve.auto.network rides its **own** public IP (Hetzner floating IP
`serve-ash-1` = 5.161.17.217 on registry-ash-1), so the edge is a dumb
`:443` → `127.0.0.1:8479` TCP forward — the raw-stream ingress (auto-9z1xh)
peeks the ClientHello SNI and routes itself, so no SNI logic and no TLS
termination live at the edge. The primary IP's single Caddy (auto/registry/
relay) is untouched. This deliberately replaces the stacked caddy-l4 `:443`
demux (auto-ot0t7) for v1; caddy-l4 remains the path only if serve is ever
consolidated back onto the shared IP or moved to anycast.

The relay never sees plaintext (raw TLS passthrough → tunnel → the operator's
local Caddy terminates with the persona wildcard cert). The forwarder→ingress
hop is loopback, but the forward prefixes each upstream connection with a
PROXY protocol v2 header (auto-p20eb) carrying the real client address, and
the registry trusts that header only from the loopback forward
(`--stream-proxy-source 127.0.0.1`), so per-source accounting sees the true
client rather than 127.0.0.1. A forward that predates PROXY v2 sends no header
and is still parsed as TLS, so upgrade ordering is free.

## Pieces (all on registry-ash-1)
- `serve_forward.py` + `autonomy-serve-forward.service` — the `:443`→`:8479`
  forward, bound to `$SERVE_BIND_IP`.
- `bind-serve-ip.sh` + `autonomy-serve-ip.service` — persist the floating IP
  on the NIC at boot (Hetzner floating IPs aren't auto-configured); the
  forwarder Requires it.
- DNS: the responder answers serve names → the floating IP via the DNS unit's
  `--relay-ip` (`DNS_RELAY_IP` in `/etc/autonomy-dns/dns.env`), set by
  `estate/dns/deploy.sh --relay-ip`.
- `/etc/autonomy-serve/serve.env`: `SERVE_BIND_IP`, `SERVE_INTERFACE`.

## Deploy / rollback
Floating IP: `hcloud floating-ip create --type ipv4 --home-location ash
--server registry-ash-1 --name serve-ash-1`. Then install the units above.
Rollback: `systemctl disable --now autonomy-serve-forward autonomy-serve-ip`,
set `DNS_RELAY_IP` back to the primary IP + restart `autonomy-registry-dns`,
and (optionally) `hcloud floating-ip delete serve-ash-1`. No change to the
primary `:443` at any point.

## Remaining for an external-browser demo
1. Persona wildcard cert via DNS-01 (auto-rvq3j) installed in the operator's
   local Caddy — the challenge TXT path is live (`dns_challenges` / bhs3c).
2. A dashboard connector serving a reservation bound to the target session:port
   (e.g. auto-0829-125521:3000), so the ingress has a live lease to route to.
