# Graph DB migrations

Operator-run scripts for one-shot schema or data shape changes that the
runtime cannot perform safely on its own. Each script is invokable as
`python3 -m tools.graph.migrations.<name>`.

## Scripts

| Script | Purpose | Bead |
|---|---|---|
| `backfill_compact_summary_role` | Re-tag pre-2026 compact-summary thoughts to `role='compact_summary'`. | (legacy) |
| `stamp_session_meta_org` | Stamp `org` (= `graph_org` or `graph_project`) into legacy-only `.session_meta.json` files under `data/agent-runs`; additive, keeps the legacy keys. Needed on any node with run dirs from before 2026-08-30, since ingest reads `org` only. | auto-5eu2s |

`migrate_to_per_org` (split `data/graph.db` into `data/orgs/<slug>.db`
per-org files, auto-9iq2s/txg5.2) ran once and was removed — it routed
rows by the `sources.project` column, which no longer exists
(auto-p6vn7 dropped it; org scoping is which database a row lives in).
Its idempotency check already refused to re-run once `data/orgs/`
existed. Disaster recovery back to a pre-split single DB goes through
the `data/graph.db.legacy-<ts>` / `data/graph.db.pre-txg5-<ts>` backup
files it left behind, not this tooling.
