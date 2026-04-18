#!/bin/bash
# Shared entrypoint for autonomy-agent:dind and its descendants.
#
# Order of operations:
#   1. Start an SSH agent and load the shared artifact key (if present)
#      BEFORE either startup.sh or the main command — the env vars need
#      to live in the parent shell so both the backgrounded startup.sh
#      and the exec'd process (claude) inherit SSH_AUTH_SOCK.
#   2. Kick off /startup.sh in the background as a single compound so
#      its exit status lands in /workspace/output/.setup-exit.
#   3. exec the caller's command.
set -e

SSH_KEY=/etc/autonomy/artifacts/id_ed25519
if [ -f "$SSH_KEY" ]; then
    eval "$(ssh-agent -s)" > /dev/null
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    if [ ! -s "$HOME/.ssh/known_hosts" ]; then
        ssh-keyscan -H github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
        chmod 644 "$HOME/.ssh/known_hosts"
    fi
    ssh-add "$SSH_KEY" 2>/dev/null || true
    export SSH_AUTH_SOCK SSH_AGENT_PID
fi

if [ -f /startup.sh ]; then
    {
        /startup.sh > /workspace/output/.setup.log 2>&1
        echo $? > /workspace/output/.setup-exit
    } &
fi
exec "$@"
