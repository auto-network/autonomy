#!/bin/sh
# Provision the in-memory (ramfs) secret stores, retrying while the Docker
# daemon comes up.
#
# A separate script so it can be RUN — by entrypoint.sh in production and by
# tests directly. The retry used to be inline in entrypoint.sh, which made it
# reachable only by starting a container.
#
# Why it retries: provisioning needs the Docker socket, so on a host reboot it
# races the daemon. On 2026-09-09 it died at 13:35:50Z on a `docker inspect`
# timeout while the daemon did not become active until 13:37:01Z. It was a
# single best-effort attempt, so the machine had NO ramfs key carrier from
# then until it was repaired by hand, and every vault unlock in that window
# had nowhere to persist.
#
# Budget: up to RAMFS_PROVISION_ATTEMPTS tries with RAMFS_PROVISION_DELAY
# between them — sleep time PLUS each attempt's own runtime, not a total.
# Exits non-zero when exhausted; the caller decides whether that is fatal.

attempts="${RAMFS_PROVISION_ATTEMPTS:-10}"
delay="${RAMFS_PROVISION_DELAY:-5}"

try=1
while [ "$try" -le "$attempts" ]; do
    if python3 -m agents.secret_ramfs; then
        exit 0
    fi
    if [ "$try" -eq "$attempts" ]; then
        echo "WARNING: secret ramfs provisioning failed after $attempts attempts — secret delivery and the key cache will fail closed until resolved" >&2
        exit 1
    fi
    echo "secret ramfs provisioning attempt $try failed (the Docker daemon may not be up yet); retrying in ${delay}s" >&2
    sleep "$delay"
    try=$((try + 1))
done
