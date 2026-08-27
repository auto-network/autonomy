#!/bin/sh
# The dispatcher's serving half, run as the non-root "autonomy" user (uid 1000)
# after deploy/entrypoint.sh has prepared the volumes and dropped privileges —
# same pattern as deploy/serve.sh, just a different long-running process.
#
# Polls for approved beads and launches session containers for them
# (agents/dispatcher.py). Needs the same code/data/orgs volumes and Docker
# socket as the dashboard service, but not the ramfs/keycache binds — the
# dispatcher never decrypts a secret itself, it only shells out to
# agents/launch.sh, which provisions each session's own secrets independently.
set -e
cd /app

exec python3 -u -m agents.dispatcher \
    --loop \
    --interval "${DISPATCH_INTERVAL:-5}" \
    --max-concurrent "${DISPATCH_MAX_CONCURRENT:-2}"
