# Autonomy home base — dashboard + graph in one sovereign image.
#
# Built FROM SOURCE out of the repo checkout: no account, token, or login
# to any registry is required. Every external fetch (base image, PyPI,
# tailwind binary) is anonymous and operator-overridable via build args,
# so the whole build can be pointed at mirrors you control. No license
# check, no phone-home. See DEPLOY.md ("Sovereign distribution").
#
#   docker compose up                # from the repo root (preferred)
#   docker build -f deploy/Dockerfile -t autonomy-node .
#
# The agent containers (agents/Dockerfile*) are a separate, optional
# image family — this image is just the always-on dashboard/graph core.

ARG BASE_IMAGE=python:3.12-slim

# ── Stage 1: assemble /app, stamp the version, strip the git REMOTE ─────────
# The build context includes .git (see .dockerignore). This stage reads the
# commit hash + commit date to stamp /app/VERSION and then removes only the git
# REMOTE — never the whole .git. The runtime image below, and the autonomy-code
# volume it seeds, keep a working local repo, because the session launcher
# `git clone`s this tree to build the read-only /workspace/repo snapshot every
# session mounts (NODE-VOLUME-MODEL.md); a tree with no .git fails that clone and
# silently strips /workspace/repo — and graph/bd/tools with it — from every
# session. No remote means the node still never phones home. Both values come
# from the commit, so the build is reproducible and no version arg is passed in.
FROM ${BASE_IMAGE} AS appsrc
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# Tailwind standalone binary (anonymous GitHub release download; override
# TAILWIND_URL to point at your own mirror). Built once below into the CSS and
# carried into the runtime image for the entrypoint's --watch.
ARG TAILWIND_VERSION=v4.3.2
ARG TAILWIND_URL=""
RUN set -e; \
    if [ -z "$TAILWIND_URL" ]; then \
        arch="$(dpkg --print-architecture)"; \
        case "$arch" in \
            amd64) t=x64 ;; \
            arm64) t=arm64 ;; \
            *) echo "unsupported arch for tailwind: $arch" >&2; exit 1 ;; \
        esac; \
        TAILWIND_URL="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-${t}"; \
    fi; \
    curl -fsSL "$TAILWIND_URL" -o /usr/local/bin/tailwindcss; \
    chmod +x /usr/local/bin/tailwindcss

COPY . /app

# Version stamp: read the commit hash + commit DATE from the checkout being
# built. Both are deterministic per commit (reproducible image); the build
# takes no version arg and nobody passes anything. Then KEEP .git (the launcher
# clones REPO_ROOT to build /workspace/repo — see NODE-VOLUME-MODEL.md), removing
# only the remote so the node never phones home, and gc'ing to shrink the pack.
RUN set -e; \
    if [ ! -d /app/.git ]; then \
        echo "ERROR: build context has no .git; the version stamp needs a git checkout (git clone, not a source tarball)." >&2; \
        exit 1; \
    fi; \
    printf 'commit=%s\ncommit_date=%s\n' \
        "$(git -C /app rev-parse HEAD)" \
        "$(git -C /app show -s --format=%cI HEAD)" \
        > /app/VERSION; \
    git -C /app remote remove origin 2>/dev/null || true; \
    git -C /app gc --prune=now --quiet

# Build the dashboard CSS once, at image build — no network at startup.
RUN /usr/local/bin/tailwindcss --cwd /app/tools/dashboard \
        -i tailwind.input.css -o static/tailwind.css --minify

# ── Stage 2: runtime image ─────────────────────────────────────────────────
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# git: graph ingest + worktree surfaces; sqlite3: operator debugging;
# curl: healthcheck; openssl: first-run TLS keypair; openssh-client: workspace
# preparation clones/fetches private repos over git+SSH (agents/workspace_manager),
# so a containerized node with no ambient ssh binary cannot launch a session that
# needs a private repo without it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git sqlite3 curl openssl ca-certificates tmux openssh-client gosu \
    && rm -rf /var/lib/apt/lists/*

# The dashboard runs as a non-root "autonomy" user (uid 1000) that MATCHES the
# uid the session agents run as — so every worktree, artifact, and clone the
# dashboard prepares is already owned by the identity the session needs, exactly
# like a dev box where dashboard and agent are the same user. The container still
# STARTS as root (the entrypoint migrates the data volume and fixes ownership),
# then drops to this user via gosu before serving. A single uid removes the whole
# class of root-vs-agent permission failures; a shared group would not (it needs
# perfect group perms on every file forever).
RUN groupadd -g 1000 autonomy \
    && useradd -u 1000 -g 1000 -m -s /bin/bash autonomy

# docker CLI (client only): a containerized node shells out to `docker` against
# the host socket (provided via a compose override) to inspect its OWN mount
# topology (agents/mount_plan.discover_topology) and to run session containers
# (agents/session_launcher). The daemon is the host's; only the client belongs
# in the image. Pinned + mirror-overridable, same pattern as the tailwind binary.
ARG DOCKER_CLI_VERSION=29.7.2
ARG DOCKER_CLI_URL=https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz
RUN curl -fsSL "$DOCKER_CLI_URL" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz --strip-components=1 -C /usr/local/bin docker/docker \
    && rm -f /tmp/docker.tgz \
    && docker --version

# bd (beads) CLI — the dashboard shells out to it for the graph bead API path,
# so a node without bd fails server-side with `bd: command not found` the moment
# that path runs (not a stale-version mismatch — an outright absence). Fetched +
# checksum-pinned from the release, same shape as agents/Dockerfile.
ARG BD_VERSION=1.2.2
ARG BD_SHA256=8140098a51d3b81d5548d1c5e6db1a2d9930e5d141efe2a4bff7d079c4d321e8
RUN curl -fsSL -o /tmp/bd.tgz \
      "https://github.com/gastownhall/beads/releases/download/v${BD_VERSION}/beads_${BD_VERSION}_linux_amd64.tar.gz" \
    && echo "${BD_SHA256}  /tmp/bd.tgz" | sha256sum -c - \
    && tar xzf /tmp/bd.tgz -C /tmp bd \
    && install -m 0755 /tmp/bd /usr/local/bin/bd \
    && rm -f /tmp/bd.tgz /tmp/bd

# dolt (required by bd — beads is Dolt-backed) — fetched + checksum-pinned.
ARG DOLT_VERSION=2.3.0
ARG DOLT_SHA256=f702aa9fc4246b076e6e255f5bb6eb6f0a120962ab8895dda9e77a0a77cfe10d
RUN curl -fsSL -o /tmp/dolt.tgz \
      "https://github.com/dolthub/dolt/releases/download/v${DOLT_VERSION}/dolt-linux-amd64.tar.gz" \
    && echo "${DOLT_SHA256}  /tmp/dolt.tgz" | sha256sum -c - \
    && tar xzf /tmp/dolt.tgz -C /tmp dolt-linux-amd64/bin/dolt \
    && install -m 0755 /tmp/dolt-linux-amd64/bin/dolt /usr/local/bin/dolt \
    && rm -rf /tmp/dolt.tgz /tmp/dolt-linux-amd64

WORKDIR /app

# Python deps first — cache layer survives source edits.
COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements.txt

# The tailwind binary the entrypoint's --watch needs at runtime; reuse the one
# the builder already fetched instead of downloading it again.
COPY --from=appsrc /usr/local/bin/tailwindcss /usr/local/bin/tailwindcss

# The assembled source tree: code + /app/VERSION + built CSS + .git (remote
# stripped in the builder stage). Owned by autonomy AT BUILD TIME (--chown) so
# the autonomy-code volume it seeds is already uid-1000-owned. The server,
# tailwind --watch, __pycache__, and the /workspace/repo git clone all run as
# autonomy — baking ownership here means the entrypoint never has to chown the
# code volume at runtime. (--chown copies + owns in one layer, no bloat.)
COPY --chown=autonomy:autonomy --from=appsrc /app /app

# graph CLI convenience wrapper (same shape as the agent images).
RUN printf '#!/bin/sh\nexec python3 -m tools.graph "$@"\n' > /usr/local/bin/graph \
    && chmod +x /usr/local/bin/graph \
    && chmod +x /app/deploy/entrypoint.sh \
    && chown autonomy:autonomy /app

# Persistent state: per-org DBs (identity + credential Settings — the
# secret store), graph.db, auth.db, TLS keypair. Its own volume, mounted at
# /app/data over the code volume. (The code volume is declared in compose, not
# here: a Dockerfile VOLUME /app would force an anonymous volume on every plain
# `docker run` and freeze image code behind it — the compose named volume is
# the deliberate, operator-controlled seat for it.)
VOLUME /app/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsk https://127.0.0.1:8080/api/ping || curl -fs http://127.0.0.1:8080/api/ping || exit 1

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
# Default command: serve the dashboard. The dispatcher service (docker-compose.yml)
# overrides this to run deploy/dispatch.sh instead, reusing the same entrypoint
# setup (docker-group grant, ramfs/keycache provisioning, ownership fixes).
CMD ["sh", "/app/deploy/serve.sh"]
