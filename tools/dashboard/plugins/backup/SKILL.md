# Backup (the `backup` plugin)

Skill revision: **2026-09-06.1** — initial: state surface, run reports,
CLI verbs. The live copy is always at `GET /api/plugins/backup/skill`.

One sentence explains the product: **a backup you can see, and a
restore you have tested** — the /backup page answers "am I safe now,
when was the last good copy, does restore actually work, what is
failing and why", in that order, from persisted state only.

## The model

- A **capture run** is one execution of the capture engine
  (tools/graph/backup-all.sh on the host; the node rolling mode when it
  lands). It writes `.backup-complete` + `run-report.json` beside the
  captured stores and a copy at `<backup root>/<tier>/latest-report.json`.
  A run is `complete` only when every required store of
  tools/data_paths.STORE_MANIFEST was captured — a missing required
  store is a failed run with reasons, never a quiet skip.
- The dashboard **reconciler** projects reports into `backup.run`
  Settings rows (machine-homed — backup state is a fact about this
  machine). Staleness derives from the age of the newest COMPLETE
  capture against `staleness_multiple × interval`: the invariant, never
  scheduler activity.
- A **restore drill** restores the latest offsite snapshot to scratch
  and proves it: integrity on every database (through the app's SQL
  function registration), row-count sanity, beads-dump count against
  the marker. Results are `backup.drill` rows. A backup nobody has
  restored is a hope.
- Failures and staleness surface in **Central attention** under the
  `backup` scope (backup_failed, backup_stale, restore_drill_failed,
  offsite_unreachable) — do not build side-channel notifications.

## Session verbs

```bash
graph backup status            # tier health, last success age, last drill
graph backup runs [--tier hourly|daily] [--limit N] [--json]
graph backup config [--json]   # effective policy (read-only from sessions)
```

Configuration writes and reconcile/drill triggers require operator
authority — a worker session reads state and reports problems; it does
not re-schedule the machine's backups.

## Rules that keep the record honest

- Never hand-write `backup.run` / `backup.drill` rows; they are the
  reconciler's and the drill runner's projections of on-disk evidence.
- Never probe the backup destination from request-path code — it is an
  NFS mount that has hung this host. Bounded probes belong to the
  reconciler and the background tasks.
- A "backup complete" claim without a marker + report is not a backup;
  say what actually happened.

## HTTP surface

```text
GET  /api/backup/summary     # derived tier health + last drill + config
GET  /api/backup/runs        # ?tier=&limit=
GET  /api/backup/drills      # ?limit=
GET  /api/backup/config
PUT  /api/backup/config      # operator authority
POST /api/backup/reconcile   # operator authority; bounded disk probe
POST /api/backup/drill       # operator authority; single-flight (409 if running)
```
