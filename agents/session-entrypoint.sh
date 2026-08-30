#!/bin/bash
# The ONE session entrypoint, baked into every session image (base, platform,
# dind, and their descendants). Nothing here is image-family-specific: it
# wires the ssh agent if a key artifact is present, runs /startup.sh in the
# background when the launcher mounted one (recording its exit in
# /workspace/output/.setup-exit — the marker the dashboard's setup wait
# reads), and execs the harness command. Whether a workspace's startup
# script runs must never depend on which image family it uses.
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

# auto-a1jco: emit a single-line setup-phase marker the host watcher reads
# (mounted to data/agent-runs/<name>-<ts>/.setup_phase). Atomic via
# tmp-then-mv; best-effort so a marker write never aborts the entrypoint.
_setup_phase() {
    if [ -d /workspace/output ]; then
        printf '%s\n' "$1" > /workspace/output/.setup_phase.tmp 2>/dev/null \
            && mv -f /workspace/output/.setup_phase.tmp /workspace/output/.setup_phase 2>/dev/null \
            || true
    fi
}

# The entrypoint is now running (past container_starting): SSH-agent setup,
# then startup.sh kickoff, then exec the harness.
_setup_phase entrypoint_running

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
        # Dependency install / image pulls begin now.
        _setup_phase setup_running
        # errexit is inherited by this backgrounded subshell: without set +e
        # a failing /startup.sh kills the compound BEFORE the exit code is
        # recorded, so .setup-exit never appears and the host can only see
        # "setup still running" — setup failure becomes unreportable.
        set +e
        /startup.sh > /workspace/output/.setup.log 2>&1
        _setup_rc=$?
        echo "$_setup_rc" > /workspace/output/.setup-exit
        # Terminal marker: without it .setup_phase reads setup_running
        # forever after a successful boot, and any late marker read looks
        # like an in-progress setup.
        if [ "$_setup_rc" = "0" ]; then
            _setup_phase setup_complete
        else
            _setup_phase setup_failed
        fi
    } &
fi
exec "$@"
