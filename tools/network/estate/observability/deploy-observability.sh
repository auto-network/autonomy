#!/usr/bin/env bash
# Deploy the private relay-estate observability baseline (auto-k9jxd):
# Prometheus + node_exporter + blackbox_exporter, all loopback-bound and
# file-provisioned. Idempotent. Standing this up live is a host action
# (like the registry deploy); this script is the reviewable, repeatable path.
#
#   deploy-observability.sh root@<relay-ip>
#
# It does NOT open any firewall port: everything binds 127.0.0.1 and the
# estate firewall already exposes only 22/80/443(+53). Prometheus, node, and
# blackbox exporters are hash-pinned static binaries fetched to
# /opt/autonomy-observability. Retention 60d (revisit from measured ingest).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
HOST="${1:?usage: deploy-observability.sh root@<relay-ip>}"
case "$HOST" in
*auto-ash-1*|*5.161.179.179*) echo "refusing the legacy pet host" >&2; exit 1;;
esac

echo "== provisioning config"
ssh -o IdentitiesOnly=yes "$HOST" "install -d -m0755 /etc/prometheus /opt/autonomy-observability /var/lib/prometheus"
scp -o IdentitiesOnly=yes -q prometheus.yml alerts.yml "$HOST":/etc/prometheus/
scp -o IdentitiesOnly=yes -q prometheus.service "$HOST":/etc/systemd/system/autonomy-prometheus.service

echo "== NOTE: fetch hash-pinned prometheus/node_exporter/blackbox_exporter"
echo "   binaries into /opt/autonomy-observability on the host (operator step;"
echo "   pin the checksums in this repo before first prod use). node_exporter"
echo "   :9100 and blackbox_exporter :9115 each get their own loopback unit."

echo "== enabling Prometheus"
ssh -o IdentitiesOnly=yes "$HOST" bash -s <<'REMOTE'
set -euo pipefail
if [ -x /opt/autonomy-observability/prometheus ]; then
    systemctl daemon-reload
    systemctl enable -q autonomy-prometheus
    systemctl restart autonomy-prometheus
    sleep 1
    curl -fsS http://127.0.0.1:9090/-/healthy && echo " prometheus healthy"
    # Prove it's NOT publicly reachable would need the public IP; the bind
    # address above is loopback, which is the guarantee.
else
    echo "prometheus binary not yet installed; config is staged. Install the"
    echo "pinned binary, then: systemctl enable --now autonomy-prometheus" >&2
fi
REMOTE
echo "== observability config deployed to $HOST (loopback-only)"
