#!/bin/bash
# Build autonomy-browser:local from a flat context holding only what the image
# copies (the repository root context would ship .git and data/).
set -euo pipefail
repo="$(cd "$(dirname "$0")/../../.." && pwd)"
context="$(mktemp -d)"
trap 'rm -rf "$context"' EXIT
for path in tools/connectors/__init__.py tools/connectors/stealth_repl.py tools/connectors/repl_auth.py \
            tools/browser_broker/__init__.py tools/browser_broker/lease_agent.py \
            tools/browser_broker/lease_watchdog.py tools/browser_broker/image/entrypoint.sh \
            tools/browser_broker/image/chrome-policy.json; do
    mkdir -p "$context/$(dirname "$path")"
    cp -p "$repo/$path" "$context/$path"
done
docker build -t "${IMAGE:-autonomy-browser:local}" -f "$repo/tools/browser_broker/image/Dockerfile" "$context"
