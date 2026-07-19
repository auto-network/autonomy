# Deploying Autonomy — environment surface

## First-run initialization

A fresh checkout becomes a working **empty** deployment with one command
(idempotent — safe to re-run any time; see `tools/init/TOOL.md`):

```bash
python -m tools.init --org myorg --org-name "My Org"
./tools/dashboard/start-dashboard.sh
```

This creates the data dirs, an empty schema'd `data/graph.db`, per-org DBs
(`personal` + your named first org) with identity Settings, the dashboard
operational DBs, the bootstrap public-surface allowlist Setting, and a
self-signed TLS keypair at `data/tls.crt`/`tls.key` (picked up automatically
by `start-dashboard.sh`; for browser-trusted certs use
`tools/dashboard/renew-tls-cert.sh` on a tailnet, or terminate TLS in a
reverse proxy / tunnel with Let's Encrypt). No seeded content is assumed or
required — search and list surfaces start out empty.

The dashboard's own startup runs the same org bootstrap, honoring
`AUTONOMY_FIRST_ORG` / `AUTONOMY_FIRST_ORG_NAME`, so exporting those before
first launch is equivalent to passing `--org`/`--org-name`.

## Sovereign distribution (Docker Compose)

The container path packages the first-run story above into one command:

```bash
git clone <source you chose> autonomy && cd autonomy
AUTONOMY_FIRST_ORG=myorg docker compose up -d
# → https://localhost:8080  (self-signed cert; accept once)
```

**Sovereign means:** the image builds from this checkout
(`deploy/Dockerfile`); no account, token, or login to any registry is
required; there is no license check and no phone-home. The only external
fetches are anonymous and happen at **build time** — the base image
(`python:3.12-slim`), PyPI wheels (`deploy/requirements.txt`), and the
tailwind binary — and each is overridable to mirrors you control via
compose env (`AUTONOMY_BASE_IMAGE`, `AUTONOMY_TAILWIND_URL`). At
**runtime** the deployment is fully self-contained: every UI library the
dashboard serves is vendored in the image
(`tools/dashboard/static/vendor/`, see `VENDOR.md` there), the CSP names
no third-party origin, and browsers never contact a CDN — a fresh
install works offline. Pinned by
`tools/dashboard/tests/test_no_cdn_dependencies.py`. Distribution is
`git clone` / tarball / an image you push to a registry *you* choose —
never a mandated one.

The container entrypoint (`deploy/entrypoint.sh`) runs `python -m
tools.init` (idempotent, honors `AUTONOMY_FIRST_ORG`) and then uvicorn,
serving HTTPS when the init-generated keypair is present (`DASHBOARD_TLS=off`
for plain HTTP behind your own proxy).

### Volume layout & backup

One named volume, `autonomy-data`, mounted at `/app/data`, holds **all**
persistent state:

| Path in volume | What it is |
|---|---|
| `orgs/<slug>.db` | per-org graph DBs — identity Settings and credential rows (**the secret store**; `orgs/personal.db` is the per-operator DB) |
| `graph.db` | main knowledge-graph DB |
| `dashboard.db`, `auth.db`, `dispatch.db`, `approval_requests.db`, `commit_workflow.db` | operational stores |
| `tls.crt`, `tls.key` | TLS keypair (self-signed by default) |
| `agent-runs/`, `session-traces/` | session artifacts |

Backing up the deployment = backing up that volume (stop the stack or use
sqlite-consistent tooling — `tools/graph/backup-*.sh` — for hot backups).
The image is disposable; the volume is not.

### Optional beads (issue tracker) backend

`docker compose --profile beads up -d` adds a `dolt` SQL-server service
(image operator-chosen via `DOLT_IMAGE`). Without it — the default — the
dashboard runs fine: beads surfaces degrade to empty with a single logged
warning. The beads DAO honors `DOLT_SQL_HOST` / `DOLT_SQL_PORT` /
`DOLT_SQL_USER` / `DOLT_SQL_PASSWORD` / `DOLT_SQL_DATABASE` for any
topology. Writing beads additionally needs the `bd` CLI, installed
separately.

Autonomy roots itself **relatively**: every core path derives from the repo
checkout (`Path(__file__)` walked up to the repo root) and the running user's
home (`Path.home()`). A clean `git clone` at *any* path boots without editing a
single file — there are no hardcoded operator host paths in `tools/` or
`agents/` (pinned by `tools/graph/tests/test_no_hardcoded_host_paths.py`).

The environment variables below are **overrides**, not requirements. Each has a
sane default derived from the checkout, so the out-of-box behaviour on a fresh
clone is correct. Set them only when your deployment layout differs from the
default (e.g. a container whose mount points don't match the host).

## Portability / path roots

| Variable | Default | Purpose |
|---|---|---|
| `AUTONOMY_ROOT` | script's repo root (`<script>/../..`) | Repo root used by the shell tooling (`tools/graph/backup-*.sh`, `tools/dashboard/renew-tls-cert.sh`). |
| `AUTONOMY_HOST_ROOT` | repo root (`Path(__file__)`-derived) | Canonical host repo prefix that container-observed session paths are rewritten to (session-trace dedup identity). |
| `AUTONOMY_HOST_HOME` | `Path.home()` | Canonical host home prefix for the same rewrite. |
| `AUTONOMY_CONTAINER_ROOT` | `/workspace/repo` | Container mount point of the repo, the *source* prefix rewritten to `AUTONOMY_HOST_ROOT`. |
| `AUTONOMY_CONTAINER_HOME` | `/home/agent` | Container home mount, the *source* prefix rewritten to `AUTONOMY_HOST_HOME`. |
| `AUTONOMY_HOST_PROJECT_ORGS` | *(unset)* | JSON object `{"<abs-cwd>": "<org-slug>"}` overlaying the Claude-Code project-dir → org routing table (`tools/graph/ingest.py`). Keys are absolute cwds; they are slugified internally. |

On the primary host these defaults already resolve to the operator's real
layout (e.g. `AUTONOMY_HOST_ROOT` → the checkout under the operator's home), so
no configuration is needed there either.

## Data & databases

| Variable | Default | Purpose |
|---|---|---|
| `AUTONOMY_ORGS_DIR` | `<repo>/data/orgs` | Per-org graph DB directory (`<slug>.db`). |
| `AUTONOMY_FIRST_ORG` | `autonomy` | Slug of the first shared org created by first-run init / dashboard startup bootstrap. |
| `AUTONOMY_FIRST_ORG_NAME` | title-cased slug | Display name seeded into the first org's `autonomy.org#1` identity Setting. |
| `GRAPH_DB` / `GRAPH_API` | `<repo>/data/graph.db` / *(unset → local DB)* | Graph DB path, or a remote graph API base URL. |
| `DASHBOARD_DB` | `<repo>/data/dashboard.db` | Dashboard overlay DB. |
| `DASHBOARD_IDENTITY_SESSION_DB` | `<repo>/data/dashboard_identity_sessions.db` | Local revocation and history store for human dashboard sessions. Keep this writable; verification fails closed if it is unavailable. |
| `DISPATCH_DB` | `<repo>/data/dispatch.db` | Dispatch state DB. |
| `APPROVAL_REQUESTS_DB` / `COMMIT_WORKFLOW_DB` | `<repo>/data/*.db` | Approval-request and commit-workflow DBs. |
| `DASHBOARD_AGENT_RUNS_DIR` | `<repo>/data/agent-runs` | Where agent-run session traces land (the container→host handoff dir). |
| `DASHBOARD_TRACE_DIR` | `<repo>/data/session-traces` | Session trace output. |

## Service / runtime

| Variable | Default | Purpose |
|---|---|---|
| `DASHBOARD_DOMAIN` | `desktop-noft5ms.tail35c24e.ts.net` | Tailscale hostname the TLS-cert renewal issues for (`renew-tls-cert.sh`). Set to your own node. |
| `DASHBOARD_URL` | `https://localhost:8080` | Dashboard base URL for CLI/tools. |
| `DASHBOARD_AUTH` | *(unset; enforced)* | Recovery kill-switch for the human unlock gate. Only `off`, `0`, `false`, or `no` (case-insensitive) disable enforcement — use to recover from a broken unlock, then unset. Unset or any other value enforces (fail-safe). |
| `DOLT_BIN` | `dolt` on `PATH` | Path to the `dolt` binary used by `backup-all.sh`. |
| `GRAPH_SCOPE` / `GRAPH_ORG` | *(unset)* | Scope graph CLI access to an org (see `graph-<project>` wrappers). |
| `AUTONOMY_SESSION` | *(set by launcher)* | Current session's tmux/routing name. |

The revocable-session migration intentionally does not grandfather old
stateless dashboard cookies. After deploying it, each open browser unlocks the
dashboard once to create its server-side session record. This is expected, not
a lost identity or passkey. `DASHBOARD_AUTH=off` remains the recovery path if
the session database is unavailable during rollout; remove the override after
the store is writable.

## Clean-clone smoke

To verify a checkout is portable, clone to a path that is neither the operator
home nor the container mount and exercise the entry points:

```bash
git clone <repo> /tmp/autonomy-clean
cd /tmp/autonomy-clean
python -m tools.graph.cli --help
python -c "import tools.dashboard.server as s; print('dashboard import OK')"
pytest tools/graph/tests/test_no_hardcoded_host_paths.py -q
```

All three must succeed with no absolute operator paths baked in.
