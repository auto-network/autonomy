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
#   AUTONOMY_FLEET_INVITE                         join an existing personal fleet
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

# The code volume (/app) needs no chown here: the image bakes it autonomy-owned
# at build time (deploy/Dockerfile, COPY --chown), so the volume seeds uid-1000.
# Only the operator-copied data/orgs (ownership we don't control) and the runtime
# keycache ramfs still need a runtime chown.

# Grant autonomy the HOST Docker group: the node launches every session as a
# host-level sibling container through this socket (see docker-compose.yml). The
# host gid varies per machine, so resolve it from the socket at runtime.
DOCKER_GID="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || true)"
if [ -n "$DOCKER_GID" ]; then
    getent group "$DOCKER_GID" >/dev/null 2>&1 || groupadd -g "$DOCKER_GID" hostdocker 2>/dev/null || true
    usermod -aG "$DOCKER_GID" autonomy 2>/dev/null || true
fi

# Provision the autonomy user's git SSH access from the operator's key artifacts
# (data/artifacts/<org>/<service>/id_*), so workspace preparation can clone
# private repos over git+SSH — the containerized-node analog of a dev box where
# the dashboard user already has keys in ~/.ssh. (Per-workspace key selection via
# the declared ANCHORE_SSH_PRIV_KEY_PATH is the longer-term refinement; this
# gives the single-key common case a clean, reproducible home.)
AUT_HOME="$(getent passwd autonomy | cut -d: -f6)"
if [ -n "$AUT_HOME" ]; then
    mkdir -p "$AUT_HOME/.ssh" && chmod 700 "$AUT_HOME/.ssh"
    for k in /app/data/artifacts/*/*/id_ed25519 /app/data/artifacts/*/*/id_rsa; do
        [ -f "$k" ] && install -m 600 "$k" "$AUT_HOME/.ssh/$(basename "$k")"
    done
    if [ -f "$AUT_HOME/.ssh/id_ed25519" ] || [ -f "$AUT_HOME/.ssh/id_rsa" ]; then
        ssh-keyscan -t ed25519,rsa github.com >> "$AUT_HOME/.ssh/known_hosts" 2>/dev/null || true
    fi
    chown -R autonomy:autonomy "$AUT_HOME/.ssh"
fi

# In-memory (ramfs) secret stores need root/the socket to provision; best-effort
# and LOUD (the vault re-checks the filesystem class on every write and fails
# closed, so a miss degrades secret features rather than downing the node).
python3 -m agents.secret_ramfs || \
    echo "WARNING: secret ramfs provisioning failed — secret delivery and the key cache will fail closed until resolved" >&2

# The key cache and delivery root are dashboard-owned memory-class stores. The
# dashboard runs as autonomy, so hand the mounted roots to it after the ramfs
# provisioner verifies them. Per-session delivery subdirectories retain their
# launcher-assigned ownership and mode 0700.
chown autonomy:autonomy /run/autonomy-keycache /run/autonomy-secrets 2>/dev/null || true

# Drop to autonomy and run whatever this container's command is (the
# dashboard's deploy/serve.sh by default — see the Dockerfile CMD — or the
# dispatcher's deploy/dispatch.sh, via the compose service's own command:
# override). The data volume is now autonomy-owned, so schema init +
# everything either process does runs as the session-agent uid. All of the
# setup above (docker-group grant, ramfs/keycache provisioning, SSH key
# staging) is identical for both; only the final process differs.
exec gosu autonomy "$@"
