#!/bin/bash
# Shared entrypoint for autonomy-agent:dind and its descendants.
#
# If a per-project /startup.sh has been mounted in, run it in the background
# while exec'ing the caller's command (claude, sh -c, etc.). The startup
# script's pid, log, and exit status land under /workspace/output so the
# agent (and CLAUDE.md) can inspect them.
set -e
if [ -f /startup.sh ]; then
    /startup.sh > /workspace/output/.setup.log 2>&1 &
    PID=$!
    echo $PID > /workspace/output/.setup.pid
    (wait $PID; echo $? > /workspace/output/.setup-exit) &
fi
exec "$@"
