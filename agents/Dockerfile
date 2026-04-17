# Autonomy Agent Container
# Bakes in all tools so agents start in <5s with zero downloads.
# Includes bd, graph, claude, agent-browser (headless Chrome), and tmux.
#
# Build:  docker build -t autonomy-agent agents/
# Run:    see agents/launch.sh

FROM ubuntu:24.04

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# System packages: Python 3.12 (ships with 24.04), git, sqlite3, node, tmux,
# plus Chrome shared-library dependencies (24 libs from ldd)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip git sqlite3 ca-certificates curl \
        nodejs npm \
        tmux \
        libglib2.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 \
        libxfixes3 libxrandr2 libxkbcommon0 libgbm1 libcairo2 libpango-1.0-0 \
        libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libdbus-1-3 libcups2 \
        libnss3 libnspr4 libasound2t64 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --break-system-packages --no-cache-dir pytest pyyaml

# ── bd CLI (Go binary, statically-ish linked — just needs libc) ──
COPY bin/bd /usr/local/bin/bd
RUN chmod +x /usr/local/bin/bd

# ── dolt (required by bd for database access) ──
COPY bin/dolt /usr/local/bin/dolt
RUN chmod +x /usr/local/bin/dolt

# ── Claude Code CLI ──
COPY bin/claude /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude

# ── graph CLI (Python module, zero third-party deps) ──
COPY graph/ /opt/autonomy/tools/graph/
RUN echo '#!/usr/bin/env bash\nexec python3 -m tools.graph "$@"' > /usr/local/bin/graph \
    && chmod +x /usr/local/bin/graph

# Set PYTHONPATH so `python3 -m tools.graph` resolves
ENV PYTHONPATH=/opt/autonomy

# ── Workspace ──
WORKDIR /workspace

# Point bd at the mounted beads directory
ENV BEADS_DIR=/data/.beads

# Verify tools are available
RUN bd --help > /dev/null 2>&1 || echo "WARN: bd not functional (expected if cross-arch)" \
    && dolt version > /dev/null 2>&1 || echo "WARN: dolt not functional" \
    && graph --help > /dev/null 2>&1 || echo "WARN: graph not functional" \
    && claude --version > /dev/null 2>&1 || echo "WARN: claude not functional" \
    && node --version > /dev/null 2>&1 || echo "WARN: node not functional"

# Agent runs as non-root
RUN userdel -r ubuntu 2>/dev/null; useradd -m -s /bin/bash -u 1000 agent \
    && mkdir -p /home/agent/.claude /home/agent/.local/bin \
    && echo '{"numStartups":1,"hasCompletedOnboarding":true,"hasSeenTasksHint":true,"hasSeenStashHint":true,"remoteDialogSeen":true,"installMethod":"native","autoUpdates":false,"projects":{"/workspace/repo":{"hasTrustDialogAccepted":true,"effortCalloutV2Dismissed":true}}}' \
       > /home/agent/.claude.json \
    && echo '{"skipDangerousModePermissionPrompt":true,"effortLevel":"high"}' \
       > /home/agent/.claude/settings.json \
    && chown -R agent:agent /home/agent/.claude /home/agent/.claude.json /home/agent/.local

# ── agent-browser + Chrome binary (needs agent user to exist) ──
RUN npm install -g agent-browser \
    && su agent -c "agent-browser install"

# ── Pre-configure agent-browser for container use ──
RUN mkdir -p /home/agent/.agent-browser \
    && echo '{"colorScheme":"dark","screenshotFormat":"png","screenshotDir":"/tmp/screenshots","args":"--no-sandbox,--disable-dev-shm-usage"}' \
       > /home/agent/.agent-browser/config.json \
    && chown -R agent:agent /home/agent/.agent-browser

RUN agent-browser --version > /dev/null 2>&1 || echo "WARN: agent-browser not functional"

USER agent

# Default entrypoint: claude with skip-permissions (dispatcher controls the prompt)
ENTRYPOINT ["claude", "--dangerously-skip-permissions"]
