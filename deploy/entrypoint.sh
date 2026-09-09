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
# The dashboard and local Caddy share only this permissioned Unix-socket
# directory. The dispatcher also runs this entrypoint but has no named volume
# here; creating an empty private directory in that container is harmless.
mkdir -p /run/autonomy-service-gateway

# First mount of a fresh, root-owned volume: hand it to autonomy once so the
# server (running as autonomy) can read/write it. After that autonomy already
# owns everything it writes, so the ownership check short-circuits and this is a
# fast no-op on every reboot — no recursive chown of a large data volume.
for d in /app/data /app/orgs /run/autonomy-service-gateway; do
    if [ "$(stat -c '%u' "$d" 2>/dev/null)" != "1000" ]; then
        chown -R autonomy:autonomy "$d" 2>/dev/null || true
    fi
done

# The code volume (/app) needs no chown here: the image bakes it autonomy-owned
# at build time (deploy/Dockerfile, COPY --chown), so the volume seeds uid-1000.
# Only the operator-copied data/orgs (ownership we don't control) and the runtime
# keycache ramfs still need a runtime chown.

# Node data-store bootstrap: create the platform's required data subdirectories
# so a FRESH node has them with no manual step. These accrete runtime state and
# are NOT shipped in the image; on sjc-2 (2026-09-08) their absence meant the
# serving-key store was missing (serve_cert_state -> key-missing for every
# scope) and the beads mount source was missing (first session launch refused,
# no container created). Idempotent, autonomy-owned; a fast no-op once present.
for d in /app/data/network /app/data/.beads; do
    mkdir -p "$d" 2>/dev/null || true
    chown autonomy:autonomy "$d" 2>/dev/null || true
done
chmod 700 /app/data/network 2>/dev/null || true

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
        # Workspace repos declare their `host` as an ssh CONFIG ALIAS
        # (github-autonomy, github-relay-cli, …). The operator's ~/.ssh/config
        # defines those on the host, but the container has none, so a clone/
        # fetch dies with "Could not resolve hostname github-autonomy" and every
        # remote-repo workspace launch fails instantly (2026-08-31). Those
        # aliases all resolve to github.com, so map github-* to it with the
        # staged key. (Distinct per-alias keys are the longer-term refinement,
        # same as the per-workspace key selection noted above.)
        _key="$AUT_HOME/.ssh/id_ed25519"; [ -f "$_key" ] || _key="$AUT_HOME/.ssh/id_rsa"
        cat > "$AUT_HOME/.ssh/config" <<SSHCFG
Host github-*
    HostName github.com
    User git
    IdentityFile $_key
    IdentitiesOnly yes
SSHCFG
        chmod 600 "$AUT_HOME/.ssh/config"
    fi
    chown -R autonomy:autonomy "$AUT_HOME/.ssh"
fi

# In-memory (ramfs) secret stores need root/the socket to provision; best-effort
# and LOUD (the vault re-checks the filesystem class on every write and fails
# closed, so a miss degrades secret features rather than downing the node).
# Provisioning is its own script so it can be run and TESTED directly rather
# than only by starting a container. It retries while the Docker daemon comes
# up; a single attempt once left this machine with no ramfs carrier for an
# hour (2026-09-09).
"$(dirname "$0")/provision-secret-ramfs.sh" || true

# The key cache is a dashboard-owned memory-class store. The dashboard runs
# as autonomy, so hand the mounted root to it after the ramfs provisioner
# verifies it. Session secret delivery is per-container-private and needs no
# host-side store at all.
chown autonomy:autonomy /run/autonomy-keycache 2>/dev/null || true

# Certificate material for the Service gateway is a child of the verified
# ramfs key cache. Caddy receives only this child, read-only, and Compose is
# told to refuse rather than create the bind source if this preparation failed.
mkdir -p /run/autonomy-keycache/service-gateway 2>/dev/null || true
chown autonomy:autonomy /run/autonomy-keycache/service-gateway 2>/dev/null || true
chmod 0700 /run/autonomy-keycache/service-gateway 2>/dev/null || true

# Host terminals: when the host's /tmp is mounted (docker-compose.yml,
# dashboard service), every tmux invocation from this container must reach
# the HOST tmux server — its socket dir lives under the host's /tmp and
# tmux derives the tmux-<uid> subdir from the calling uid by itself.
# Detection, not configuration: the mount's presence IS the signal.
if [ -d /host-tmp ]; then
    export TMUX_TMPDIR=/host-tmp
fi

# Path identity for session records: this container's repo is always /app;
# the host-side form of the same tree derives from the relocated data root
# when the operator set one. Derived here as startup OUTPUTS — nobody
# passes these as inputs (see tools/graph/ingest.py:_session_path_rewrites).
export AUTONOMY_CONTAINER_ROOT="${AUTONOMY_CONTAINER_ROOT:-/app}"
if [ -n "${AUTONOMY_HOST_DATA_ROOT:-}" ] && [ -z "${AUTONOMY_HOST_ROOT:-}" ]; then
    export AUTONOMY_HOST_ROOT="${AUTONOMY_HOST_DATA_ROOT}/code"
fi

# Drop to autonomy and run whatever this container's command is (the
# dashboard's deploy/serve.sh by default — see the Dockerfile CMD — or the
# dispatcher's deploy/dispatch.sh, via the compose service's own command:
# override). The data volume is now autonomy-owned, so schema init +
# everything either process does runs as the session-agent uid. All of the
# setup above (docker-group grant, ramfs/keycache provisioning, SSH key
# staging) is identical for both; only the final process differs.
exec gosu autonomy "$@"
