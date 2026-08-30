#!/usr/bin/env bash
# Push this checkout's committed HEAD into a running node's code volume.
#
#   deploy/update-node.sh                 # default project: autonomy-trial
#   deploy/update-node.sh <compose-project> [ref]
#
# The node hot-reloads from its code volume (deploy/serve.sh runs uvicorn
# --reload), so updating the checkout IS the deploy. No container restart, no
# image rebuild, no registry.
#
# Why a bundle rather than `git pull`: the node has no git remote (it must not
# phone home) and its code volume is not readable from the host without root.
# `git bundle create -` writes to stdout and `docker exec -i` reads stdin, so
# the objects stream straight in with no temp file on either side.
#
# Why `-u autonomy`: `docker exec` defaults to root. Running git as root
# rewrites .git/objects, the refs, and every restored file as root:root inside
# a tree owned by autonomy. Nothing fails at the time — the files stay
# world-readable — and the next git operation as the app user is what breaks.
set -euo pipefail

PROJECT="${1:-autonomy-trial}"
REF="${2:-HEAD}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "refusing: this checkout has uncommitted changes." >&2
    echo "The node gets committed history only — a bundle cannot carry a dirty tree," >&2
    echo "so an uncommitted fix would appear to deploy and silently not be there." >&2
    exit 1
fi

CONTAINER="$(docker ps -q \
    --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=dashboard")"
if [[ -z "$CONTAINER" ]]; then
    echo "refusing: no running dashboard container in compose project '${PROJECT}'." >&2
    echo "Running projects:" >&2
    # `docker ps --format` exposes .Labels as a STRING, so `index` on it throws;
    # .Label "<key>" is the accessor that works here.
    docker ps --format '{{.Label "com.docker.compose.project"}}' \
        | grep -v '^$' | sort -u | sed 's/^/  /' >&2
    exit 1
fi

BEFORE="$(docker exec -u autonomy "$CONTAINER" git -C /app rev-parse --short HEAD 2>/dev/null || echo unknown)"
TARGET="$(git rev-parse --short "$REF")"

git bundle create - "$REF" 2>/dev/null | docker exec -i -u autonomy "$CONTAINER" sh -c '
    set -e
    cat > /tmp/update.bundle
    cd /app
    git fetch -q /tmp/update.bundle '"$REF"'
    git reset -q --hard FETCH_HEAD
    rm -f /tmp/update.bundle
'

AFTER="$(docker exec -u autonomy "$CONTAINER" git -C /app rev-parse --short HEAD)"
echo "${PROJECT}: ${BEFORE} -> ${AFTER}"

# data/uploads/.gitkeep is force-added on purpose (auto-j3oj3): a checkout
# without it leaves runc no mount point inside the read-only /workspace/repo
# snapshot and every session launch fails. The reset restores it; say so, because
# an operator who sees it reappear should not delete it as stray.
docker exec -u autonomy "$CONTAINER" test -f /app/data/uploads/.gitkeep \
    && echo "  data/uploads/.gitkeep present (required for session launches)" \
    || echo "  WARNING: data/uploads/.gitkeep missing — session launches will fail" >&2
