#!/usr/bin/env bash
# Install reproducible coturn artifacts. Activation is a separate explicit step
# because it requires a provisioned secret, certificate, limits, DNS, firewall,
# and the second public IPv4.
set -euo pipefail

TARGET=${1:?usage: deploy.sh <ssh-target> [--activate]}
ACTIVATE=${2:-}
if [ -n "$ACTIVATE" ] && [ "$ACTIVATE" != --activate ]; then
    echo "unknown argument: $ACTIVATE" >&2
    exit 1
fi
case "$TARGET" in
*5.161.179.179* | *auto-ash-1*)
    echo "refusing to deploy coturn to the legacy pet host" >&2
    exit 1
    ;;
esac

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IMAGE='coturn/coturn@sha256:75e9ebd1e19005bec0c7f591d29afe22f959916ac8d9c852452f27db8c789828'

echo "==> install docker, certbot, and coturn deployment artifacts"
ssh "$TARGET" 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io certbot'
ssh "$TARGET" 'getent group autonomy-coturn >/dev/null || groupadd --system autonomy-coturn; install -d -m 0755 /opt/autonomy-coturn; install -d -m 0700 /etc/autonomy-coturn'
rsync -az --delete --exclude runtime.env.example "$HERE/" "$TARGET:/opt/autonomy-coturn/"
scp -q "$HERE/runtime.env.example" "$TARGET:/etc/autonomy-coturn/runtime.env.example"
scp -q "$HERE/autonomy-coturn.service" "$TARGET:/etc/systemd/system/autonomy-coturn.service"
ssh "$TARGET" "gid=\$(getent group autonomy-coturn | cut -d: -f3); sed -i \"s/^TURN_RUNTIME_GID=.*/TURN_RUNTIME_GID=\$gid/\" /etc/autonomy-coturn/runtime.env.example; chmod 0755 /opt/autonomy-coturn/*.sh /opt/autonomy-coturn/*.py; chmod 0644 /etc/systemd/system/autonomy-coturn.service /etc/autonomy-coturn/runtime.env.example; docker pull '$IMAGE'; systemd-analyze verify /etc/systemd/system/autonomy-coturn.service; systemctl daemon-reload"

if [ "$ACTIVATE" != --activate ]; then
    echo "==> staged only; service remains disabled and stopped"
    exit 0
fi

ssh "$TARGET" bash -s <<'EOF'
set -euo pipefail
for path in \
    /etc/autonomy-coturn/runtime.env \
    /etc/autonomy-coturn/turn-rest-secrets \
    /etc/letsencrypt/live/turn.auto.network/fullchain.pem \
    /etc/letsencrypt/live/turn.auto.network/privkey.pem
do
    test -s "$path" || { echo "missing activation prerequisite: $path" >&2; exit 1; }
done
. /etc/autonomy-coturn/runtime.env
test "$TURN_RUNTIME_GID" = "$(getent group autonomy-coturn | cut -d: -f3)"
test "$(getent ahostsv4 turn.auto.network | awk 'NR == 1 {print $1}')" = "$TURN_PUBLIC_IP"
systemctl enable --now autonomy-coturn.service
sleep 2
curl -fsS http://127.0.0.1:9641/metrics >/dev/null
systemctl --no-pager --lines=20 status autonomy-coturn.service
EOF
