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
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# git: graph ingest + worktree surfaces; sqlite3: operator debugging;
# curl: healthcheck + build-time fetches; openssl: first-run TLS keypair.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git sqlite3 curl openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first — cache layer survives source edits.
COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements.txt

# Tailwind standalone binary (anonymous GitHub release download; override
# TAILWIND_URL to point at your own mirror). Used once below to build the
# dashboard CSS, and kept on PATH for operators who run the dev launcher.
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

# Source tree (data/, .git, venvs etc. excluded via .dockerignore — the
# persistent state lives in the /app/data volume, never in the image).
COPY . /app

# Build the dashboard CSS once, at image build — no network at startup.
RUN /usr/local/bin/tailwindcss --cwd /app/tools/dashboard \
        -i tailwind.input.css -o static/tailwind.css --minify

# graph CLI convenience wrapper (same shape as the agent images).
RUN printf '#!/bin/sh\nexec python3 -m tools.graph "$@"\n' > /usr/local/bin/graph \
    && chmod +x /usr/local/bin/graph \
    && chmod +x /app/deploy/entrypoint.sh

# Version stamp. The code volume ships as bare FILES (no .git, no remote — the
# node never phones home), so it can't self-report its version. Bake the seed
# commit and build time here; this answers "what code is this node running?"
# for debugging/repro at zero cost, with no history in the image or volume. The
# file seeds into the autonomy-code volume with the rest of /app.
#
# commit is a real SHA (deploy/publish-images.sh and deploy/build.sh pass one)
# or the honest marker `source` for a bare from-source build — never empty and
# never "unknown", which would carry no information into the long-lived volume.
# build time is always real: passed in, or captured here at build.
ARG AUTONOMY_VERSION=source
ARG AUTONOMY_BUILD_TIME=
RUN v="$AUTONOMY_VERSION"; t="$AUTONOMY_BUILD_TIME"; \
    [ -n "$t" ] || t="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; \
    echo "$v" | grep -Eq '^(source|[0-9a-f]{40})$' || { \
        echo "ERROR: AUTONOMY_VERSION must be a 40-hex commit SHA or the literal 'source', got '$v'." >&2; exit 1; }; \
    echo "$t" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' || { \
        echo "ERROR: AUTONOMY_BUILD_TIME must be UTC YYYY-MM-DDTHH:MM:SSZ, got '$t'." >&2; exit 1; }; \
    printf 'commit=%s\nbuilt=%s\n' "$v" "$t" > /app/VERSION

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
