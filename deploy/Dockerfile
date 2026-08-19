# Autonomy home base — dashboard + graph in one sovereign image.
#
# Built FROM SOURCE out of the repo checkout: no account, token, or login
# to any registry is required. Every external fetch (base image, PyPI,
# tailwind binary) is anonymous and operator-overridable via build args,
# so the whole build can be pointed at mirrors you control. No license
# check, no phone-home. See DEPLOY.md ("Sovereign distribution").
#
#   docker compose up                # from the repo root (preferred)
#   docker build -f deploy/Dockerfile -t autonomy-dashboard .
#
# The agent containers (agents/Dockerfile*) are a separate, optional
# image family — this image is just the always-on dashboard/graph core.

ARG BASE_IMAGE=python:3.12-slim

# ── Stage 1: assemble /app, read the version from .git, then drop .git ──────
# The build context includes .git (see .dockerignore). This stage reads the
# commit hash + commit date to stamp /app/VERSION and then removes .git, so the
# runtime image below — and the autonomy-code volume it seeds — ship bare files
# with no history and no remote. Both values come from the commit, so the build
# is reproducible and no version arg is ever passed in.
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
# takes no version arg and nobody passes anything. Then drop .git.
RUN set -e; \
    if [ ! -d /app/.git ]; then \
        echo "ERROR: build context has no .git; the version stamp needs a git checkout (git clone, not a source tarball)." >&2; \
        exit 1; \
    fi; \
    printf 'commit=%s\ncommit_date=%s\n' \
        "$(git -C /app rev-parse HEAD)" \
        "$(git -C /app show -s --format=%cI HEAD)" \
        > /app/VERSION; \
    rm -rf /app/.git

# Build the dashboard CSS once, at image build — no network at startup.
RUN /usr/local/bin/tailwindcss --cwd /app/tools/dashboard \
        -i tailwind.input.css -o static/tailwind.css --minify

# ── Stage 2: runtime image ─────────────────────────────────────────────────
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# git: graph ingest + worktree surfaces; sqlite3: operator debugging;
# curl: healthcheck; openssl: first-run TLS keypair.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git sqlite3 curl openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first — cache layer survives source edits.
COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements.txt

# The tailwind binary the entrypoint's --watch needs at runtime; reuse the one
# the builder already fetched instead of downloading it again.
COPY --from=appsrc /usr/local/bin/tailwindcss /usr/local/bin/tailwindcss

# The assembled source tree: code + /app/VERSION + built CSS, and NO .git
# (dropped in the builder stage). This is exactly what seeds the autonomy-code
# volume on a containerized node.
COPY --from=appsrc /app /app

# graph CLI convenience wrapper (same shape as the agent images).
RUN printf '#!/bin/sh\nexec python3 -m tools.graph "$@"\n' > /usr/local/bin/graph \
    && chmod +x /usr/local/bin/graph \
    && chmod +x /app/deploy/entrypoint.sh

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
