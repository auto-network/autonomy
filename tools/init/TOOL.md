# tools/init — First-Run Initialization

Idempotent bootstrap that turns a fresh checkout on a clean machine into a
working **empty** deployment, from nothing (bead auto-q1fsp, H3; design
graph://dc310166-911).

```bash
python -m tools.init                              # defaults: first org 'autonomy'
python -m tools.init --org acme --org-name "Acme Corp"
python -m tools.init --json                       # machine-readable report
```

## What it produces

| Step | Result |
|------|--------|
| `dir:data/*` | `data/`, `data/orgs/`, `data/agent-runs/`, `data/session-traces/` |
| `graph.db` | empty `data/graph.db` with the full canonical schema |
| `org:<slug>`, `org:personal` | per-org DBs: one operator-named first org + `personal`, each with bootstrap `orgs` row and `autonomy.org#1` identity Setting |
| `dashboard.db` … `commit_workflow.db` | dashboard-side operational sqlite stores |
| `setting:bootstrap-allowlist` | `autonomy.org.bootstrap-allowlist#1` in `personal.db`, seeded from `tools/graph/curation/autonomy-bootstrap-allowlist.yaml` — records the upstream reference surface without shipping content (ties to bead auto-mu1n1) |
| `tls` | self-signed keypair at `data/tls.crt` + `data/tls.key` (`start-dashboard.sh` auto-detects the pair) |

## Guarantees

- **Idempotent** — every step detects pre-existing state and leaves it
  untouched; a second run reports `exists`/`skipped` everywhere and
  `InitReport.changed == False`. Safe to run at every startup.
- **No content assumptions** — zero pre-existing graph rows is the expected
  state. Search and list surfaces on a fresh deployment return empty, not
  errors.
- **No host-state dependence** — everything derives from the checkout path,
  env overrides (see `DEPLOY.md`), and the operator-supplied org name.

## First org naming

Resolution order: `--org` flag → `AUTONOMY_FIRST_ORG` env → `autonomy`.
Display name: `--org-name` → `AUTONOMY_FIRST_ORG_NAME` → title-cased slug.
The dashboard's startup bootstrap (`org_ops.ensure_bootstrap_orgs`) honors
the same env vars, so setting them before first launch is equivalent to
running the CLI.

## TLS

The self-signed pair is good enough for LAN/tailnet HTTPS (browsers warn
once; PWA install requires accepting it). For browser-trusted certs:

- **Tailscale**: `tools/dashboard/renew-tls-cert.sh` issues a cert for your
  tailnet hostname (`DASHBOARD_DOMAIN`).
- **Let's Encrypt via tunnel/reverse proxy**: terminate TLS in front
  (Caddy, nginx + certbot, or a cloudflared/`tailscale serve` tunnel) and
  run the dashboard behind it; delete `data/tls.crt`/`tls.key` to serve
  plain HTTP to the proxy.

Never overwrites an existing pair; a half-pair (only one of crt/key) is
reported and left for the operator.

## Library API

```python
from tools.init import initialize
report = initialize(root, first_org="acme", tls=False)
assert not initialize(root, first_org="acme", tls=False).changed  # no-op
```
