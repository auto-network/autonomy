# Autonomy Agent Container
# Bakes in all tools so agents start in <5s with zero downloads.
# No services, no DinD. Just a clean workspace with bd, graph, claude.
#
# Build:  docker build -t autonomy-agent agents/
# Run:    see agents/launch.sh

FROM ubuntu:24.04

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# System packages: Python 3.12 (ships with 24.04), git, sqlite3
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        git \
        sqlite3 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── bd CLI (Go binary, statically-ish linked — just needs libc) ──
COPY bin/bd /usr/local/bin/bd
RUN chmod +x /usr/local/bin/bd

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

# Verify tools are available
RUN bd --help > /dev/null 2>&1 || echo "WARN: bd not functional (expected if cross-arch)" \
    && graph --help > /dev/null 2>&1 || echo "WARN: graph not functional" \
    && claude --version > /dev/null 2>&1 || echo "WARN: claude not functional"

# Agent runs as non-root
RUN useradd -m -s /bin/bash agent \
    && mkdir -p /home/agent/.claude \
    && echo '{"numStartups":1,"installMethod":"native","autoUpdates":false,"hasSeenTasksHint":true}' \
       > /home/agent/.claude.json \
    && echo '{"skipDangerousModePermissionPrompt":true}' \
       > /home/agent/.claude/settings.json \
    && chown -R agent:agent /home/agent/.claude /home/agent/.claude.json
USER agent

# Default entrypoint: claude with skip-permissions (dispatcher controls the prompt)
ENTRYPOINT ["claude", "--dangerously-skip-permissions"]
