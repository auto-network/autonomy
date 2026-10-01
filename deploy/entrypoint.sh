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
#   DASHBOARD_PLAIN_PORT                          plain-HTTP listener in the same worker
#                                                 (serve.sh: 8081; "off" disables)
#   DASHBOARD_TLS=off                             skip TLS keypair + serve plain HTTP
#   DASHBOARD_DOMAIN                              CN/SAN for the self-signed cert
set -e
cd /app

# Org-mount root (the autonomy-orgs volume mounts here).
mkdir -p /app/orgs /app/attachments
# The dashboard and local Caddy share only this permissioned Unix-socket
# directory. The dispatcher also runs this entrypoint but has no named volume
# here; creating an empty private directory in that container is harmless.
mkdir -p /run/autonomy-service-gateway

# First mount of a fresh, root-owned volume: hand it to autonomy once so the
# server (running as autonomy) can read/write it. After that autonomy already
# owns everything it writes, so the ownership check short-circuits and this is a
# fast no-op on every reboot — no recursive chown of a large data volume.
for d in /app/data /app/orgs /app/attachments /run/autonomy-service-gateway; do
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

# The autonomy user's git SSH access, so workspace preparation can clone private
# repos over git+SSH. The KEY comes only from the vault (auto-zhbje): the
# dashboard releases the audited entry node.github-ssh-key into its ramfs key
# cache at NODE_SSH_KEY whenever the vault is unlocked (tools/dashboard/
# node_ssh.py). Nothing is copied from disk any more -- the old loop over
# data/artifacts/*/*/id_* left a plaintext key in every container sharing this
# entrypoint -- and copies an earlier start left behind are removed. A role
# without the key cache (tmux, dispatcher, relay) simply has no identity.
AUT_HOME="$(getent passwd autonomy | cut -d: -f6)"
NODE_SSH_KEY=/run/autonomy-keycache/node-ssh/id_ed25519
if [ -n "$AUT_HOME" ]; then
    mkdir -p "$AUT_HOME/.ssh" && chmod 700 "$AUT_HOME/.ssh"
    rm -f "$AUT_HOME/.ssh/id_ed25519" "$AUT_HOME/.ssh/id_rsa"
    {
        ssh-keyscan -t ed25519,rsa github.com >> "$AUT_HOME/.ssh/known_hosts" 2>/dev/null || true
        # Workspace repos declare their `host` as an ssh CONFIG ALIAS
        # (github-autonomy, github-relay-cli, …). The operator's ~/.ssh/config
        # defines those on the host, but the container has none, so a clone/
        # fetch dies with "Could not resolve hostname github-autonomy" and every
        # remote-repo workspace launch fails instantly (2026-08-31). Those
        # aliases all resolve to github.com, so map github-* (and github.com
        # itself, which used to fall back to the copied default key) to it
        # with the released key. (Distinct per-alias keys are the longer-term
        # refinement.)
        cat > "$AUT_HOME/.ssh/config" <<SSHCFG
Host github-* github.com
    HostName github.com
    User git
    IdentityFile $NODE_SSH_KEY
    IdentitiesOnly yes
SSHCFG
        chmod 600 "$AUT_HOME/.ssh/config"
    }
    chown -R autonomy:autonomy "$AUT_HOME/.ssh"
fi

# In-memory (ramfs) secret stores need root/the socket to provision; best-effort
# and LOUD (the vault re-checks the filesystem class on every write and fails
# closed, so a miss degrades secret features rather than downing the node).
# Provisioning is its own script so it can be run and TESTED directly rather
# than only by starting a container. It retries while the Docker daemon comes
# up; a single attempt once left this machine with no ramfs carrier for an
# hour (2026-09-09).
#
# ROLE GATE: this entrypoint is shared by the dashboard and the dispatcher, but
# only the dashboard holds the keycache/ramfs mount. The dispatcher deliberately
# has no keycache bind (deploy/dispatch.sh: "the dispatcher never decrypts a
# secret itself") — so on it /run/autonomy-keycache is a plain dir, and the
# provisioner FATALs "not ramfs" and burns its full retry budget on every start
# for setup the dispatcher does not use. Set AUTONOMY_PROVISION_RAMFS=0 on any
# role without a keycache mount to skip it. Default 1 preserves dashboard/node
# behavior exactly.
if [ "${AUTONOMY_PROVISION_RAMFS:-1}" = "1" ]; then
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

    # The MCP relay's credentials (auto-5gdao) are released into this child
    # once the vault is unlocked. It exists from boot -- empty while the vault
    # is cold -- so the relay's read-only bind (create_host_path: false)
    # always resolves: relay.sh then waits, and the restart policy carries it
    # until the release lands, instead of docker refusing to start it at all.
    # Likewise the voice gateway's token (auto-es7ja), bound as only this
    # child. The dispatcher's token (dispatcher/) needs no pre-made child: the
    # dispatcher binds the whole key cache and the dashboard creates it.
    for child in mcp-relay voice; do
        mkdir -p "/run/autonomy-keycache/$child" 2>/dev/null || true
        chown autonomy:autonomy "/run/autonomy-keycache/$child" 2>/dev/null || true
        chmod 0700 "/run/autonomy-keycache/$child" 2>/dev/null || true
    done
else
    echo "entrypoint: ramfs keycache provisioning skipped (AUTONOMY_PROVISION_RAMFS=0; role has no keycache mount)" >&2
fi

# The node's tmux socket directory (docker-compose.yml, autonomy-tmux volume):
# the `tmux` sidecar serves on it and the dashboard is its client. Detection,
# not configuration: the mount's presence IS the signal. A fresh named volume
# mounts root-owned; tmux (uid 1000) must be able to create tmux-1000 in it.
if [ -d /run/autonomy-tmux ]; then
    chown autonomy:autonomy /run/autonomy-tmux 2>/dev/null || true
    export TMUX_TMPDIR=/run/autonomy-tmux
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
