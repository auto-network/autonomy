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
#   5. run deploy/smoke.py against the PUBLIC url -- /healthz only proves
#      the process started; the smoke test proves a link still works
#
# Set SMOKE_LINK to a real link URL to include the guest path (envelope,
# handshake, artifact header). Without it the smoke test says out loud that
# it did not prove a link works.
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

echo "==> syncing code and install primer to $TARGET:$APP_DIR"
ssh "$TARGET" "mkdir -p $APP_DIR/tools/network $APP_DIR/deploy"
rsync -az --delete --exclude '__pycache__' --exclude 'tests' \
    "$REPO_ROOT/tools/network/idkit" \
    "$REPO_ROOT/tools/network/relaykit" \
    "$REPO_ROOT/tools/network/registry" \
    "$TARGET:$APP_DIR/tools/network/"
rsync -az --delete \
    "$REPO_ROOT/deploy/install" \
    "$TARGET:$APP_DIR/deploy/"

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

SMOKE_URL="${SMOKE_URL:-https://relay.auto.network}"
echo "==> smoke test against $SMOKE_URL"
SMOKE_ARGS=("$SMOKE_URL")
if [ -n "${SMOKE_LINK:-}" ]; then
    SMOKE_ARGS+=(--link "$SMOKE_LINK")
fi
# The smoke's HTTP rungs are stdlib-only, so any python3 runs those. The GUEST
# path speaks the real channel protocol and needs websockets, which the system
# python3 on the estate box does not have -- a bare `python3` made the smoke die
# with ModuleNotFoundError *after* the service had restarted, so a successful
# deploy printed a traceback and read as an outage. Probe for what THIS run
# actually needs, so a link-less run is never blocked on a dep it will not use.
pick_python() {
    local candidate
    # A session worktree has no .venv of its own. Walk up from the checkout to
    # find one -- worktrees live under <primary-checkout>/data/worktrees/..., so
    # the primary checkout's .venv is a few levels up. (--git-common-dir does NOT
    # help here: it points at the managed bare clone, which has no venv.)
    local venvs=() dir="$REPO_ROOT"
    while [ "$dir" != "/" ]; do
        [ -x "$dir/.venv/bin/python" ] && venvs+=("$dir/.venv/bin/python")
        dir="$(dirname "$dir")"
    done
    local probe="import sys"
    [ -n "${SMOKE_LINK:-}" ] && probe="import websockets"
    for candidate in "${PYTHON:-}" "${venvs[@]}" python3 python; do
        [ -n "$candidate" ] || continue
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c "$probe" 2>/dev/null; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

if ! SMOKE_PYTHON="$(pick_python)"; then
    echo "ERROR: no python that can run the guest path -- cannot prove a link works." >&2
    echo "       The service HAS been restarted; this is a missing test dep, NOT a bad deploy." >&2
    echo "       Fix: pip install websockets, or set PYTHON=/path/to/venv/bin/python," >&2
    echo "       or unset SMOKE_LINK to run the stdlib-only rungs alone." >&2
    exit 1
fi

# Deliberately NOT tolerated: a deploy that leaves links broken has failed,
# even though systemd is happy and /healthz answers.
"$SMOKE_PYTHON" "$REPO_ROOT/tools/network/registry/deploy/smoke.py" "${SMOKE_ARGS[@]}"

echo "==> deployed: registry live on $TARGET (loopback :8477, fronted by Caddy)"
