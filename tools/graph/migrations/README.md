# Graph DB migrations

Operator-run scripts for one-shot schema or data shape changes that the
runtime cannot perform safely on its own. Each script is invokable as
`python3 -m tools.graph.migrations.<name>`.

## Scripts

| Script | Purpose | Bead |
|---|---|---|
| `backfill_compact_summary_role` | Re-tag pre-2026 compact-summary thoughts to `role='compact_summary'`. | (legacy) |
| `migrate_to_per_org` | Split `data/graph.db` into `data/orgs/<slug>.db` per-org files. | auto-9iq2s (txg5.2) |

## `migrate_to_per_org`

Splits the legacy single-DB world into per-org DBs. Required precondition
for downstream write routing (auto-36v11), curation pass (auto-txg5.0)
and cross-org read enablement (auto-txg5.4).

### Run

```bash
# 1. Stop the dashboard + dispatcher.
./tools/dashboard/stop-dashboard.sh
./agents/stop-dispatcher.sh   # or: kill $(cat data/dispatcher.pid)

# 2. Inspect the partition plan first (no DBs touched).
python3 -m tools.graph.migrations.migrate_to_per_org --dry-run

# 3. Run for real. Backups are written to data/graph.db.pre-txg5-<ts>
#    siblings; the legacy DB is renamed to data/graph.db.legacy-<ts>
#    on success and left in place for the operator to delete later.
python3 -m tools.graph.migrations.migrate_to_per_org

# 4. Restart services. Dashboard / dispatcher pick up data/orgs/*.db
#    via resolve_caller_db_path (which falls back to legacy when the
#    per-org file is absent — so partial state is recoverable).
./tools/dashboard/start-dashboard.sh
./agents/start-dispatcher.sh
```

### Pre-flight checks

The script refuses to proceed when:

* `data/dashboard.pid` or `data/dispatcher.pid` reference a running
  process. Stop them first.
* The legacy DB backup fails. The migration must not touch source data
  without an intact recovery copy.
* `data/orgs/autonomy.db` already has rows AND a `data/graph.db.legacy-*`
  sibling exists (idempotent refusal — the migration has already run).

### Rollback

If anything looks wrong after the migration:

```bash
# Stop dashboard + dispatcher.
./tools/dashboard/stop-dashboard.sh
./agents/stop-dispatcher.sh

# Remove the per-org DBs.
rm -rf data/orgs/

# Restore the legacy DB from the rename.
mv data/graph.db.legacy-<ts> data/graph.db
# WAL/SHM siblings (if present) follow the same naming.

# Restart services.
./tools/dashboard/start-dashboard.sh
./agents/start-dispatcher.sh
```

The pre-migration backup at `data/graph.db.pre-txg5-<ts>` is a second
recovery copy; rollback prefers the rename (it's the unmodified
original) and keeps the backup as a safety net until the operator
deletes both manually after a stability period.

### Partition rules

| Source | Where it lands |
|---|---|
| `sources` row with `project='X'` (X in target orgs) | `<X>.db` |
| `sources` row with NULL `project` | `autonomy.db` |
| `sources` row with `project='Y'` (Y not in target orgs) | `autonomy.db` (warning logged) |
| `thoughts`, `derivations`, `claims`, `attachments`, `note_*`, `captures` | follow source |
| `edges` | follow source endpoint's parent source |
| `entity_mentions` | follow content (thought/derivation) → source |
| `entities` | copied to every org that references via mentions |
| `tags` | duplicated to every org (global vocab) |
| `threads`, `nodes`, `node_refs`, `settings` | `autonomy.db` (operator-local / global) |
| `captures` with NULL source | `personal.db` |

### Verification

After the copy phase the script verifies:

* Per-table row sums across per-org DBs equal the legacy total
  (excluding `tags`, which is duplicated by design).
* `thoughts_fts` and `derivations_fts` row counts match the base
  tables in every per-org DB (FTS5 triggers populated correctly).
* Every per-org DB opened cleanly.

A non-zero exit code means verification failed — the operator should
investigate before restarting services. The per-org DBs are left in
place so the issue is debuggable; rollback (above) returns to the
pre-migration state.
