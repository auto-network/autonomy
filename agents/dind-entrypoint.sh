#!/bin/bash
# Shared entrypoint for autonomy-agent:dind and its descendants.
#
# If a per-project /startup.sh has been mounted in, run it in the background
# as a single shell unit so the exit status can be captured in the same
# process that ran the script. The startup script's log and exit status
# land under /workspace/output so the agent (and CLAUDE.md) can inspect
# them.
set -e
if [ -f /startup.sh ]; then
    {
        /startup.sh > /workspace/output/.setup.log 2>&1
        echo $? > /workspace/output/.setup-exit
    } &
fi
exec "$@"
