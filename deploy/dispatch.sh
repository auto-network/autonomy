#!/bin/sh
# The dispatcher's serving half, run as the non-root "autonomy" user (uid 1000)
# after deploy/entrypoint.sh has prepared the volumes and dropped privileges —
# same pattern as deploy/serve.sh, just a different long-running process.
#
# Polls for approved beads and launches session containers for them
# (agents/dispatcher.py). Needs the same code/data/orgs volumes and Docker
# socket as the dashboard service, and a READ-ONLY view of the dashboard's key
# cache for its scoped token (dispatcher/token, re-minted by the dashboard at
# every start; auto-es7ja). It never decrypts a secret itself: it shells out to
# agents/launch.sh, which provisions each session's own secrets independently.
set -e
cd /app

exec python3 -u -m agents.dispatcher \
    --loop \
    --interval "${DISPATCH_INTERVAL:-5}" \
    --max-concurrent "${DISPATCH_MAX_CONCURRENT:-2}"
