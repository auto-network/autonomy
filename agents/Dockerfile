# Autonomy Agent Container
# Bakes in all tools so agents start in <5s with zero downloads.
# Includes bd, graph, claude, codex, agent-browser (headless Chrome), and tmux.
#
# Build:  docker build -t autonomy-agent agents/
# Run:    see agents/launch.sh

FROM ubuntu:24.04

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# System packages: Python 3.12 (ships with 24.04), git, sqlite3, node, tmux,
# plus Chrome shared-library dependencies (24 libs from ldd)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip git sqlite3 ca-certificates curl rsync \
        openssh-client gnupg \
        nodejs npm \
        gh \
        jq \
        tmux \
        libglib2.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 \
        libxfixes3 libxrandr2 libxkbcommon0 libgbm1 libcairo2 libpango-1.0-0 \
        libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libdbus-1-3 libcups2 \
        libnss3 libnspr4 libasound2t64 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --break-system-packages --no-cache-dir pytest pytest-xdist pyyaml python-gnupg cryptography "pydantic>=2,<3"

# ── bd CLI (Go binary, statically-ish linked — just needs libc) ──
COPY bin/bd /usr/local/bin/bd
RUN chmod +x /usr/local/bin/bd

# ── dolt (required by bd for database access) ──
COPY bin/dolt /usr/local/bin/dolt
RUN chmod +x /usr/local/bin/dolt

# ── Claude Code CLI ──
COPY bin/claude /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude

# Commit-signing shim (git gpg.program). Worktree setup points gpg.program
# here when a workspace's policy requires a GPG signature; needs python3 + curl
# (both present above).
COPY commit_sign_shim.sh /usr/local/bin/commit-sign-shim
RUN chmod +x /usr/local/bin/commit-sign-shim

# ── graph CLI wrapper (resolves tools.graph via runtime repo mount) ──
# The repo is bind-mounted at /workspace/repo by session_launcher, so there is
# no need to bake a second copy of tools/graph/ into the image — see
# graph://af0deb88-d86 for the baked-subtree anti-pattern.
RUN echo '#!/usr/bin/env bash\nexec python3 -m tools.graph "$@"' > /usr/local/bin/graph \
    && chmod +x /usr/local/bin/graph

# Point PYTHONPATH at the runtime mount so `tools.graph` and `agents` both resolve.
ENV PYTHONPATH=/workspace/repo

# ── Workspace ──
WORKDIR /workspace

# Point bd at the mounted beads directory and codex at its container-local home.
ENV BEADS_DIR=/data/.beads
ENV CODEX_HOME=/home/agent/.codex

# Capability command shims: the launcher bind-mounts per-session shims at this
# path (session_launcher.CAPABILITY_BIN_DIR) — on PATH so enabled capability
# commands (jira-read, mermaid-share, …) are directly invocable.
ENV PATH="/etc/autonomy/cap-bin:${PATH}"

# Verify tools are available
RUN bd --help > /dev/null 2>&1 || echo "WARN: bd not functional (expected if cross-arch)" \
    && dolt version > /dev/null 2>&1 || echo "WARN: dolt not functional" \
    && graph --help > /dev/null 2>&1 || echo "WARN: graph not functional" \
    && claude --version > /dev/null 2>&1 || echo "WARN: claude not functional" \
    && node --version > /dev/null 2>&1 || echo "WARN: node not functional"

# Agent runs as non-root
RUN userdel -r ubuntu 2>/dev/null; useradd -m -s /bin/bash -u 1000 agent \
    && mkdir -p /home/agent/.claude /home/agent/.codex /home/agent/.agents /home/agent/.local/bin \
    # claude is COPYed to /usr/local/bin (above), but .claude.json declares
    # installMethod=native, so Claude Code's startup self-check (/doctor) looks
    # for the binary at ~/.local/bin/claude and prints a "missing or broken"
    # setup warning when it's absent. Symlink it so the native-install path
    # resolves and the boot warning goes away.
    && ln -sf /usr/local/bin/claude /home/agent/.local/bin/claude \
    && echo '{"numStartups":1,"hasCompletedOnboarding":true,"hasSeenTasksHint":true,"hasSeenStashHint":true,"remoteDialogSeen":true,"resumeReturnDismissed":true,"installMethod":"native","autoUpdates":false,"projects":{"/workspace/repo":{"hasTrustDialogAccepted":true,"effortCalloutV2Dismissed":true}}}' \
       > /home/agent/.claude.json \
    && echo '{"skipDangerousModePermissionPrompt":true,"effortLevel":"high","disableArtifact":true,"permissions":{"deny":["AskUserQuestion","ExitPlanMode","Artifact"]}}' \
       > /home/agent/.claude/settings.json \
    && chown -R agent:agent /home/agent/.claude /home/agent/.codex /home/agent/.agents /home/agent/.claude.json /home/agent/.local

# ── Codex CLI + agent-browser (needs node + agent user to exist) ──
# Codex is an npm package plus platform-specific optional dependency, not a
# standalone host ELF like Claude, so install it inside the image directly.
ARG CODEX_VERSION=0.146.0
RUN npm install -g "@openai/codex@${CODEX_VERSION}" agent-browser \
    && su agent -c "agent-browser install"

# ── Pre-configure agent-browser for container use ──
RUN mkdir -p /home/agent/.agent-browser \
    && echo '{"colorScheme":"dark","screenshotFormat":"png","screenshotDir":"/tmp/screenshots","args":"--no-sandbox,--disable-dev-shm-usage"}' \
       > /home/agent/.agent-browser/config.json \
    && chown -R agent:agent /home/agent/.agent-browser

RUN codex --version > /dev/null 2>&1 || echo "WARN: codex not functional" \
    && agent-browser --version > /dev/null 2>&1 || echo "WARN: agent-browser not functional"

USER agent

# Default entrypoint: claude with skip-permissions (dispatcher controls the prompt)
ENTRYPOINT ["claude", "--dangerously-skip-permissions"]
