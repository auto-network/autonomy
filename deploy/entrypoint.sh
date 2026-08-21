#!/bin/sh
# Home-base container entrypoint. Starts as ROOT to prepare the mounted volumes
# and grant socket access, then drops to the non-root "autonomy" user (uid 1000)
# via gosu to serve (deploy/serve.sh). autonomy is the SAME uid the session
# agents run as, so every worktree/artifact/clone the dashboard prepares is owned
# by the identity the sessions use — the single-uid model that removes the whole
# class of root-vs-agent permission failures on a containerized node.
#
# Environment (all optional — see DEPLOY.md):
#   AUTONOMY_FIRST_ORG / AUTONOMY_FIRST_ORG_NAME  first org naming
#   AUTONOMY_INVITE                               join an existing org
#   DASHBOARD_HOST / DASHBOARD_PORT               bind address (default 0.0.0.0:8080)
#   DASHBOARD_TLS=off                             skip TLS keypair + serve plain HTTP
#   DASHBOARD_DOMAIN                              CN/SAN for the self-signed cert
set -e
cd /app

# Org-mount root (the autonomy-orgs volume mounts here).
mkdir -p /app/orgs

# First mount of a fresh, root-owned volume: hand it to autonomy once so the
# server (running as autonomy) can read/write it. After that autonomy already
# owns everything it writes, so the ownership check short-circuits and this is a
# fast no-op on every reboot — no recursive chown of a large data volume.
for d in /app/data /app/orgs; do
    if [ "$(stat -c '%u' "$d" 2>/dev/null)" != "1000" ]; then
        chown -R autonomy:autonomy "$d" 2>/dev/null || true
    fi
done

# Grant autonomy the HOST Docker group: the node launches every session as a
# host-level sibling container through this socket (see docker-compose.yml). The
# host gid varies per machine, so resolve it from the socket at runtime.
DOCKER_GID="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || true)"
if [ -n "$DOCKER_GID" ]; then
    getent group "$DOCKER_GID" >/dev/null 2>&1 || groupadd -g "$DOCKER_GID" hostdocker 2>/dev/null || true
    usermod -aG "$DOCKER_GID" autonomy 2>/dev/null || true
fi

# In-memory (ramfs) secret stores need root/the socket to provision; best-effort
# and LOUD (the vault re-checks the filesystem class on every write and fails
# closed, so a miss degrades secret features rather than downing the node).
python3 -m agents.secret_ramfs || \
    echo "WARNING: secret ramfs provisioning failed — secret delivery and the key cache will fail closed until resolved" >&2

# Drop to autonomy and serve. The data volume is now autonomy-owned, so schema
# init + everything the server does runs as the session-agent uid.
exec gosu autonomy sh /app/deploy/serve.sh
