# Relay-estate observability baseline (auto-k9jxd)

Private Prometheus + a fixed alert catalogue, scraping the relay estate's
own metrics endpoints and a blackbox probe of the public serve path. This is
the V1.0 baseline slice of `auto-k9jxd`; the operator-facing view is the
dashboard **Operations** plugin decided in `graph://41195143-344` (NOT
Grafana). Notification delivery to the operator's device is the follow-on
`auto-xap1u` — this slice makes alert STATE observable, not paged.

## What it is
- `prometheus.yml` — scrape config (file-provisioned, no click-config):
  registry `:9479`, DNS process `:9480`, node_exporter `:9100`, coturn
  `:9641` (when enabled), and a blackbox probe of the public serve URL.
- `alerts.yml` — TargetDown, ServeEdgeDown, ServeCertExpiringSoon (<21d, off
  the blackbox TLS probe — the cert lives in the operator's local Caddy, not
  the registry), ReconnectStorm, NoLiveTunnels, disk/inode pressure.
- `prometheus.service` — loopback-bound (`127.0.0.1:9090`), 60d retention.
- `deploy-observability.sh` — idempotent, config-provisioning deploy.

## Privacy / reachability
Everything binds `127.0.0.1`. The estate firewall opens only 22/80/443
(+53 on the relay host), so Prometheus, the exporters, and the metrics
endpoints are unreachable from the public internet by construction — the
"no public metrics listener" constraint holds at both the bind and the
firewall. The dashboard backend queries Prometheus over the operator-private
path; the browser never receives the Prometheus address.

## Standing it up (host action, like the registry deploy)
```bash
cd tools/network/estate/observability
./deploy-observability.sh root@<relay-ip>
# then install the hash-pinned prometheus/node_exporter/blackbox_exporter
# binaries into /opt/autonomy-observability and enable their loopback units.
```
Retention (60d) is a starting point; resize from measured ingest once the
series count is known. Enable coturn's native `prometheus` listener in
`turnserver.conf` to light up the `coturn` target (until then its TargetDown
is the intended "not wired yet" signal).

## Interim view before the Operations panel lands
The dashboard Operations panel is the dashboard lane's piece. Until it ships,
the private Prometheus expression browser (reachable only over the
operator-private path) is the sanctioned ad-hoc view — the O0 decision
explicitly blesses this.
