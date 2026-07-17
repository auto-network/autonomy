#!/usr/bin/env bash
# Deploy the auto.network registry to a scripted-estate host.
#
# Target: the NEW deterministic hcloud estate (graph note 8cb2a39c-4bc —
# cattle, not pets). The legacy hand-configured box auto-ash-1
# (5.161.179.179) is explicitly refused: nothing new lands there.
#
# Usage:
#   deploy/deploy.sh <ssh-target>          # e.g. root@registry.auto.network
#
# What it does (idempotent):
#   1. rsync tools/network/{idkit,registry} to /opt/autonomy-registry,
#      preserving the tools.network package path
#   2. create a venv and install runtime deps (fastapi, uvicorn, cryptography)
#   3. install + enable the systemd unit, restart the service
#   4. probe /healthz through the loopback bind
#
# TLS/routing is the estate's Caddy front (reverse_proxy 127.0.0.1:8477);
# this script deliberately does not touch it.

set -euo pipefail

TARGET="${1:?usage: deploy.sh <ssh-target>}"

case "$TARGET" in
*5.161.179.179* | *auto-ash-1*)
    echo "refusing to deploy to the legacy pet host auto-ash-1 (see graph 8cb2a39c-4bc)" >&2
    exit 1
    ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
APP_DIR=/opt/autonomy-registry

echo "==> syncing code to $TARGET:$APP_DIR"
ssh "$TARGET" "mkdir -p $APP_DIR/tools/network"
rsync -az --delete --exclude '__pycache__' --exclude 'tests' \
    "$REPO_ROOT/tools/network/idkit" \
    "$REPO_ROOT/tools/network/relaykit" \
    "$REPO_ROOT/tools/network/registry" \
    "$TARGET:$APP_DIR/tools/network/"

echo "==> venv + dependencies"
ssh "$TARGET" bash -s <<EOF
set -euo pipefail
cd $APP_DIR
if [ ! -x venv/bin/python ]; then
    python3 -m venv venv
fi
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet 'fastapi>=0.110' 'uvicorn>=0.29' 'cryptography>=42' 'websockets>=13'
EOF

echo "==> systemd unit"
scp -q "$REPO_ROOT/tools/network/registry/deploy/autonomy-registry.service" \
    "$TARGET:/etc/systemd/system/autonomy-registry.service"
ssh "$TARGET" bash -s <<'EOF'
set -euo pipefail
systemctl daemon-reload
systemctl enable --now autonomy-registry
systemctl restart autonomy-registry
sleep 1
curl -fsS http://127.0.0.1:8477/healthz
echo
systemctl --no-pager --lines=5 status autonomy-registry
EOF

echo "==> deployed: registry live on $TARGET (loopback :8477, fronted by Caddy)"
