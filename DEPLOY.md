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
| `DISPATCH_DB` | `<repo>/data/dispatch.db` | Dispatch state DB. |
| `APPROVAL_REQUESTS_DB` / `COMMIT_WORKFLOW_DB` | `<repo>/data/*.db` | Approval-request and commit-workflow DBs. |
| `DASHBOARD_AGENT_RUNS_DIR` | `<repo>/data/agent-runs` | Where agent-run session traces land (the container→host handoff dir). |
| `DASHBOARD_TRACE_DIR` | `<repo>/data/session-traces` | Session trace output. |

## Service / runtime

| Variable | Default | Purpose |
|---|---|---|
| `DASHBOARD_DOMAIN` | `desktop-noft5ms.tail35c24e.ts.net` | Tailscale hostname the TLS-cert renewal issues for (`renew-tls-cert.sh`). Set to your own node. |
| `DASHBOARD_URL` | `https://localhost:8080` | Dashboard base URL for CLI/tools. |
| `DOLT_BIN` | `dolt` on `PATH` | Path to the `dolt` binary used by `backup-all.sh`. |
| `GRAPH_SCOPE` / `GRAPH_ORG` | *(unset)* | Scope graph CLI access to an org (see `graph-<project>` wrappers). |
| `AUTONOMY_SESSION` | *(set by launcher)* | Current session's tmux/routing name. |

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
