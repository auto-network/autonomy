# Enterprise NG agent environment

You are running inside the `autonomy-agent:enterprise-ng` container. The
entrypoint wrapper kicks off `/startup.sh` in the background at boot; it
starts a nested Docker daemon, marks the worktrees as `safe.directory` for
git, and runs `poetry install` in `/workspace/enterprise_ng`.

## Worktrees

- `/workspace/enterprise` — Anchore Enterprise (read-only, for cross-repo lookups)
- `/workspace/enterprise_ng` — Anchore Enterprise NG (writable)

## Check that startup finished before running project commands

Background setup writes to `/workspace/output/.setup.log` and records its
exit status in `/workspace/output/.setup-exit` when done. Before running
Poetry/make/docker-compose commands, wait for (or verify) completion:

```bash
while [ ! -f /workspace/output/.setup-exit ]; do sleep 1; done
exit_code=$(cat /workspace/output/.setup-exit)
if [ "$exit_code" != "0" ]; then
    echo "startup failed — see /workspace/output/.setup.log"
    tail -50 /workspace/output/.setup.log
    exit 1
fi
```

If `.setup-exit` is missing, setup is still running. Check `.setup.log`
for progress. If it is non-zero, read the log and halt — don't try to work
around a broken environment.

## Docker-in-Docker

A full Docker daemon runs inside this container (storage driver `vfs`,
`--tls=false`). Use `docker`, `docker compose`, etc. normally.

## Graph scoping

`GRAPH_SCOPE=anchore` is set so notes and searches default to the Anchore
project boundary. `GRAPH_TAGS=enterprise,enterprise-ng` is pre-applied to
notes you create.
