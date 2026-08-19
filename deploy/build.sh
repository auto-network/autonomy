#!/usr/bin/env bash
# Supported sovereign source build: stamp a REAL /app/VERSION (seed commit SHA
# + UTC build time) into the node image, then run docker compose with your args.
#
#   deploy/build.sh up -d                       # build + run, stamped with HEAD
#   AUTONOMY_FIRST_ORG=myorg deploy/build.sh up -d
#   deploy/build.sh build                        # build only
#
# The node ships its code as bare files (no .git in the image or the code
# volume), so the commit can only be captured HERE, at build, from the checkout
# you build from. The bare `docker compose up -d` one-liner still works and
# stays sovereign; it just stamps commit=source (a from-source build, real
# build time, commit not captured). Use THIS wrapper when you want the running
# node to report its exact commit.
#
# This is additive: it does not replace the sovereign one-liner, it just carries
# the SHA the one-liner cannot compute on its own.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ -z "${AUTONOMY_VERSION:-}" ]]; then
    if git -C "$REPO_ROOT" rev-parse HEAD >/dev/null 2>&1; then
        AUTONOMY_VERSION="$(git -C "$REPO_ROOT" rev-parse HEAD)"
    else
        echo "ERROR: $REPO_ROOT is not a git checkout; set AUTONOMY_VERSION=<40-hex commit> explicitly" >&2
        exit 2
    fi
fi
AUTONOMY_BUILD_TIME="${AUTONOMY_BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

# Fail closed on garbage: this wrapper's whole point is a REAL commit, so the
# version must be a 40-hex SHA (never the bare-compose `source` marker), and the
# time must be a UTC timestamp. A bad override must not silently reach the image.
if [[ ! "$AUTONOMY_VERSION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: AUTONOMY_VERSION must be a 40-hex commit SHA, got '$AUTONOMY_VERSION'" >&2
    exit 2
fi
if [[ ! "$AUTONOMY_BUILD_TIME" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
    echo "ERROR: AUTONOMY_BUILD_TIME must be UTC YYYY-MM-DDTHH:MM:SSZ, got '$AUTONOMY_BUILD_TIME'" >&2
    exit 2
fi
export AUTONOMY_VERSION AUTONOMY_BUILD_TIME

exec docker compose -f "$REPO_ROOT/docker-compose.yml" "$@"
