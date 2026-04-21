## Scope

This is the v5-only workspace. It mounts the enterprise repo at
`/workspace/enterprise` and nothing else. For full-stack NG work that needs
both repos plus component_catalog tooling, dispatch the bead to
**enterprise-ng** instead.

v5 is a subset of NG — anything you do here will work in NG too, but NG has
additional services and tooling (component_catalog, the NG Taskfile, the
NG dev-compose stack) that are not present in v5 and not in this container.

## Container vs Host Differences

This container is NOT the same as working on the host. Key differences:

- **Postgres client + Go + make** are pre-installed for the v5 build flow.
- **DinD**: a nested dockerd is started by `startup.sh`. Compose stacks
  launched inside the container run on this nested daemon, not on the host.
- **Repos**: only `/workspace/enterprise` is mounted, on a per-session
  `agent/<bead>` worktree. Edit and commit normally — your branch is
  collected when the dispatch finishes.
- **Build cache**: `poetry install` runs at startup so the first command
  doesn't pay the install cost. The enterprise repo's Makefile activates
  `.venv/bin/activate` before calling poetry; the venv is pre-created.

## v5 work conventions

- v5 patch branches live on release lines (e.g. `v5.27.x`). When working a
  patch bead, branch off the appropriate release line, not master.
- Compose / test stacks live under `/workspace/enterprise/` — use the v5
  Makefile targets (`make build`, `make test`, etc.). NG-specific Taskfile
  targets are not available here.
- Anchore Enterprise license: NOT mounted in this workspace by default
  (NG mounts it for the dev-compose stack). If a v5 test stack needs the
  license, add `license.yaml` as a workspace artifact in the workspace
  Setting and re-launch.

## When to switch to enterprise-ng

Use enterprise-ng when the bead:
- touches `enterprise_ng/` or anything under `component_catalog/`,
- needs the NG dev-compose stack (`task dev-up`, NG service deps),
- spans both repos in a single change.

When in doubt, dispatch to enterprise-ng — it is the superset.
