# Backup (the `backup` plugin)

Skill revision: **2026-09-29.1** — the run records its own row; no
reconciler, no background loops, no inbox alerts. Prior: 2026-09-06.1
(initial: state surface, run reports, CLI verbs). The live copy is always at `GET /api/plugins/backup/skill`.

One sentence explains the product: **a backup you can see, and a
restore you have tested** — the /backup page answers "am I safe now,
when was the last good copy, does restore actually work, what is
failing and why", in that order, from persisted state only.

## The model

- A **capture run** is one execution of the capture engine
  (tools/graph/backup-all.sh on the host; the node rolling mode when it
  lands). It writes `.backup-complete` + `run-report.json` beside the
  captured stores and a copy at `<data root>/backup-reports/<tier>-latest.json` (on the shared data volume, so a containerized node sees it without the NAS mount).
  A run is `complete` only when every required store of
  tools/data_paths.STORE_MANIFEST was captured — a missing required
  store is a failed run with reasons, never a quiet skip.
- The run **records its own result**: as soon as the report is written
  (and again when the offsite verdict is stamped), backup-all.sh runs
  `python -m tools.dashboard.plugins.backup.record <tier>`, which writes
  the `backup.run` Settings row (machine-homed — backup state is a fact
  about this machine). Staleness derives from the age of the newest COMPLETE
  capture against `staleness_multiple × interval`: the invariant, never
  scheduler activity.
- A **restore drill** restores the latest offsite snapshot to scratch
  and proves it: integrity on every database (through the app's SQL
  function registration), row-count sanity, beads-dump count against
  the marker. Drills run on demand; each writes one `backup.drill` row
  when it ends (a drill in flight is known only to the dashboard process
  running it). A backup nobody has restored is a hope.
- Failures and staleness are shown on the /backup page.

## Session verbs

```bash
graph backup status            # tier health, last success age, last drill
graph backup runs [--tier hourly|daily] [--limit N] [--json]
graph backup config [--json]   # effective policy (read-only from sessions)
```

Configuration writes and drill triggers require operator
authority — a worker session reads state and reports problems; it does
not re-schedule the machine's backups.

## Rules that keep the record honest

- Never hand-write `backup.run` / `backup.drill` rows; they are written
  by the backup run and the drill runner.
- Never probe the backup destination from request-path code — it is an
  NFS mount that has hung this host.
- A "backup complete" claim without a marker + report is not a backup;
  say what actually happened.

## HTTP surface

```text
GET  /api/backup/summary     # derived tier health + last drill + config
GET  /api/backup/runs        # ?tier=&limit=
GET  /api/backup/drills      # ?limit=
GET  /api/backup/config
PUT  /api/backup/config      # operator authority
POST /api/backup/drill       # operator authority; single-flight (409 if running)
```
