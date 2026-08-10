# Reactive Policy Evaluation: Current State, Failure Modes, and Redesign

**Date**: 2026-05-23
**Context**: Investigation triggered by the nightly migration run on 2026-05-22 21:26-22:05 UTC, which produced 569 of 572 mapping rows in a "both flags set" state, 14-minute non-winner job times, and a 9-minute mid-run stall.

---

## 1. Executive Summary

The current policy evaluation system has a clean data model — content-addressed `PolicyFinding` rows keyed by `(rule, vuln_tuple)`, with per-scope `PolicyFindingLink` rows that allow read-time aggregation via the `Assets` table. Aggregation is correct; storage is correct.

What's wrong lives entirely in the **execution layer**:

1. **Duplicate workflow dispatch** from a TOCTOU race in `create_work_queue`, with no abort path in `migrate-one-sbom`. Both duplicate workflows march independently through the 5-step chain.
2. **Artifact-level policy evaluation has no cache**. Every invocation re-streams the SBOM's vuln tuples and re-runs the policy executable, even when the inputs are unchanged.
3. **Metadata-only feed updates don't trigger re-evaluation**. A CVE being added to KEV, a severity escalation, or an EPSS shift won't update existing findings until an unrelated event (asset add, policy edit) drags the affected app_version through evaluation by luck.
4. **The metadata loader uses `TRUNCATE + INSERT`** for KEV, EPSS, and vuln_score tables — destroying the prior state in the same transaction, making it impossible to detect what actually changed.
5. **`policy_digest` is stamped on every link row**, forcing full link-table rewrites on cosmetic policy edits that didn't actually change any rule.

The proposed redesign moves to a fully reactive model:

- `artifact_uuid → set(policy_id)` cache (bounded by Assets count)
- `policy_id → set(rule_digest)` mapping (one row per policy, updated in place)
- `PolicyFindingLink` carries `rule_digest`, not `policy_digest`
- Metadata loader switches to merge-with-`RETURNING`, emitting a `vuln_metadata_changes` queue
- Evaluation is triggered per-event (new `PackageVulnerability`, vuln metadata change, policy edit, app `active_policy` change, asset add) with O(relevant rules × event) cost

Per-event work bounded by the rate of change, not by the cardinality of existing data.

---

## 2. Current Architecture

### 2.1 SBOM Import Workflow (5 steps)

Definition: `legacy_sbom_migration.py:373-393` and `sbom_workflow.py:295-318`.

```
┌─────────────────┐    ┌──────────────────────────┐    ┌─────────────────────────────────┐
│ decompose       │ ─▶ │ scan                     │ ─▶ │ ensure-app-version-policy-eval  │
│ add-sbom-asset  │    │ vulnerability-scan-art.. │    │                                 │
└─────────────────┘    └──────────────────────────┘    └─────────────────────────────────┘
                                                                       │
                                                                       ▼
                                                        ┌─────────────────────────────┐
                                                        │ policy                      │
                                                        │ artifact-policy-evaluation  │
                                                        └─────────────────────────────┘
                                                                       │
                                                                       ▼
                                                        ┌─────────────────────────────┐
                                                        │ activate                    │
                                                        │ activate-asset              │
                                                        └─────────────────────────────┘
```

**Step 1: `add-sbom-asset`** (`sbom_workflow.py:114`)
- Parses the SBOM bytes; creates `Artifact` and `ArtifactPackage` rows in NG.
- Has `serialize_on="dedup_key"` (mutex on the SBOM content hash).
- Dedup-hit path returns the existing `artifact_uuid` and exits early — but does NOT call `ctx.complete_workflow()`, so the workflow continues.

**Step 2: `vulnerability-scan-artifact`** (`scanner_worker.py:549`)
- Pool: `vuln_scanner` (size 1 per pod).
- Calls `get_scan_units_for_artifact` (`dao/vulnerability_scan.py:1580`), which anti-joins against `PackageVulnerability` and `EmptyScan` to find only unscanned `(pm, distro)` pairs.
- For an artifact whose packages have already been scanned by some prior asset, this returns an empty list; the scan loop breaks; the handler returns success — but does NOT call `ctx.complete_workflow()`.

**Step 3: `ensure-app-version-policy-evaluation`** (`policy_evaluation.py:312`)
- Calls `try_claim_policy_evaluation` keyed on `(app_version_uuid, policy_id, policy_digest)`.
- If row is `COMPLETE` for that key → returns immediately (cache hit).
- If newly claimed → runs `_run_full_app_version_evaluation`, which calls `executable.execute(reference, context)` with `artifact_id=None`. The SQL function `app_version_vuln_report` requires an `Assets` row linking artifact to app_version; the new artifact isn't activated yet (step 5), so this iterates over **only previously-activated artifacts**.

**Step 4: `artifact-policy-evaluation`** (`policy_evaluation.py:471`)
- Looks up the parent `PolicyEvaluation` row from step 3.
- Calls `executable.execute(reference, context)` with `artifact_id=<new_artifact_uuid>`.
- `app_version_vuln_report` with `_artifact_uuid IS NOT NULL` bypasses the `Assets` check — evaluates the new artifact's vuln tuples directly.
- Writes findings via `FindingsBatchWriter`, scoped to this artifact.
- Has NO cache. Re-runs from scratch every time.

**Step 5: `activate-asset`** (`sbom_workflow.py:205`)
- Creates the `component_catalog_assets` row linking artifact to app_version.
- Two paths: `update_asset_artifact` (if `asset_id` given — updates existing) or `add_asset` (creates new). Migration takes the create path; both duplicate workflows have `asset_id=None`.
- The unique constraint `(account_name, app_version_uuid, name)` catches the second of any duplicate pair.

### 2.2 Feed Update Workflow (4 steps)

Definition: `feed_update_workflow.py:482-490`.

```
┌──────────────────────────┐    ┌─────────────────────────────┐
│ process-feed-update      │ ─▶ │ vulnerability-scan-batch    │
│ (download, diff, load)   │    │ (rescan affected packages)  │
└──────────────────────────┘    └─────────────────────────────┘
            │                                  │
            └──────────────┬───────────────────┘
                           ▼
            ┌─────────────────────────────────┐
            │ emit-vulnerability-events       │
            └─────────────────────────────────┘
                           │
                           ▼
            ┌─────────────────────────────────────┐
            │ enqueue-stale-policy-evaluations    │
            └─────────────────────────────────────┘
```

**Step 1: `process-feed-update`** (`feed_update_workflow.py:170`)
- Acquires `ctx.set_phase("feed_update", exclusive=True)` — a Postgres session-level advisory lock keyed on `(job_type, phase)`.
- Calls `bootstrap_databases()` which downloads the grype DB to local pod disk if `local_checksum != remote_checksum`.
- If `prev_vulnerability_db_path` is None (e.g., fresh container after morning reset) OR `scan_mode == FULL_ONLY` → full metadata reload + `_create_full_rescan_queue()` → `ctx.complete_workflow()`. No diff is computed.
- Else → `run_db_diff(result)` → calls grype CLI to produce `{packages, vulnerabilities, databases}`.

**Step 2: `vulnerability-scan-batch`** (`scanner_worker.py:561`)
- Pool: same `vuln_scanner` (contends with `vulnerability-scan-artifact`).
- Reads `items` from input data — the `packages` list from the diff.
- Scans only the affected `(pm_id, distro_id)` tuples.

**Step 3: `emit-vulnerability-events`**
- Generates per-app events about vulnerability changes.

**Step 4: `enqueue-stale-policy-evaluations`** (`feed_update_workflow.py:361`)
- Reads `vulnerability_changes` from input (the `packages` list from step 1).
- Calls `get_app_versions_for_scan_units(vulnerability_changes)` — joins changed scan units back to app_versions that contain them.
- Inserts a PENDING placeholder `PolicyEvaluation` row per affected app_version.
- Enqueues one `ENSURE_APP_VERSION_POLICY_EVALUATION` job per affected app_version.

### 2.3 Data Model

```
┌────────────────────────────────────────────────┐
│ PolicyFinding                                  │
│   uuid (PK)                                    │
│   finding_key                                  │  ← sha256(serialized_rule + sorted(vuln_link.object_id))
│   rule_id, gate, trigger, trigger_id            │
│   action (stop|warn|go)                         │
│   detail_digest → PolicyFindingDetail          │
│   UNIQUE INDEX (finding_key)                    │
└────────────────────────────────────────────────┘
                  │
                  │ finding_uuid
                  ▼
┌────────────────────────────────────────────────┐
│ PolicyFindingLink                              │
│   id (PK)                                       │
│   finding_uuid                                  │
│   policy_id, policy_name, policy_digest         │  ← STAMPED on every link
│   link_type (app_version|artifact|vulnerability)│
│   object_id (polymorphic string)                │
│   allowlist_*, suppressed_until                 │
└────────────────────────────────────────────────┘

┌────────────────────────────────────────────────┐
│ PolicyEvaluation                                │
│   id (PK)                                       │
│   app_version_uuid                              │
│   policy_id, policy_digest                      │  ← cache key for ensure-step
│   status (pending|in_progress|complete|failed)  │
│   evaluating_job_id                             │
│   ix_policy_evaluations_in_flight (UNIQUE)      │  ← serialization primitive
└────────────────────────────────────────────────┘
```

**`PolicyFinding`**: content-addressed by `finding_key`. The same rule firing on the same `(pm, vuln, distro)` from N different artifacts collapses to one row. Artifact identity is NOT in `finding_key`.

**`PolicyFindingLink`**: per-target, per-policy. Three link types via polymorphic `object_id`:
- `app_version` → `str(AppVersion.uuid)`
- `artifact` → `str(Artifact.uuid)`
- `vulnerability` → `"{pm_id}:{vuln_db_id}:{distro_id}"`

`policy_id` and `policy_digest` are stamped on every link row.

### 2.4 Aggregation

**Write-time aggregation** lives in the SQL function `app_version_vuln_report` (`app_version_vuln_report.sql:54-123`):

```sql
SELECT ...
       array_agg(DISTINCT art.uuid) AS artifact_uuids,
       ...
FROM component_catalog_package_vulnerabilities pv
    JOIN component_catalog_package_metadata pm ON ...
    JOIN component_catalog_packages p ON ...
    JOIN component_catalog_artifact_packages ap ON ...
    JOIN component_catalog_artifacts art ON ...
    LEFT JOIN component_catalog_assets assets ON
        art.uuid = assets.artifact_uuid AND
        assets.account_name = _account_name AND
        assets.app_version_uuid = _version_uuid
    JOIN component_catalog_vulnerabilities v ON ...
    JOIN component_catalog_providers prov ON ...
WHERE NOT ap.exclude_vulns
    AND pv.distro_id = art.distro_id
    AND (
        (_artifact_uuid IS NOT NULL AND art.uuid = _artifact_uuid)
        OR (_artifact_uuid IS NULL AND assets.uuid IS NOT NULL)
    )
GROUP BY ...
```

Returns one row per `(vuln, package_metadata, distro, provider)` tuple with `artifact_uuids` aggregated. Dedup happens in SQL before the policy engine sees anything.

**Read-time aggregation** for app_version policy results lives in `dao/policy.py:130-170`:

```python
# Direct: APP_VERSION links
direct = select(PolicyFindingLink.finding_uuid).where(
    PolicyFindingLink.policy_id == policy_id,
    PolicyFindingLink.policy_digest == policy_digest,
    PolicyFindingLink.link_type == FindingLinkType.APP_VERSION,
    PolicyFindingLink.object_id == str(app_version_uuid),
)

# Indirect: ARTIFACT links joined to Assets via artifact_uuid
indirect = (
    select(PolicyFindingLink.finding_uuid)
    .join(Assets, Assets.artifact_uuid == PolicyFindingLink.object_id.cast(Uuid))
    .where(
        PolicyFindingLink.policy_id == policy_id,
        PolicyFindingLink.policy_digest == policy_digest,
        PolicyFindingLink.link_type == FindingLinkType.ARTIFACT,
        Assets.app_version_uuid == app_version_uuid,
    )
)

return direct.union(indirect)
```

The app_version's finding set is the UNION of direct `app_version` links + `artifact` links joined through `Assets`. The "view" is computed at read time; no write-time rollup is needed.

### 2.5 What Actually Gets Written at Each Scope

For typical vulnerability policies, the breakdown is:
- `VulnerabilityMatchTrigger` (`vulnerabilities.py:241`) fires per `(vuln, package, distro)` with ONE vulnerability link + N artifact links (one per artifact containing it). **No app_version link.**
- `AlwaysFireTrigger` (`always.py:12`) emits one app_version link. Used for testing/denylist.
- `FeedOutOfDateTrigger` (`vulnerabilities.py:642`): artifact links on the normal path; app_version link only on the exception branch.

Result: in nightly's data, 0 app_version-scoped link rows out of 21,159 total. App_version queries hit the indirect (artifact-via-Assets) path exclusively.

---

## 3. Failure Modes Observed in the 2026-05-22 Migration Run

### 3.1 Duplicate Workflow Dispatch (TOCTOU)

**Cause**: `bootstrap_legacy_sbom_migration_queue` checks `_active_migrate_queue_exists`, then calls `create_work_queue`. There is no DB-level guard (no partial unique index on `job_work_queues(job_type) WHERE status='active'`). Two CC pods slipped through the check 51 ms apart at startup and both successfully inserted their queue.

**Evidence**: `job_work_queues` rows `id=1` created `21:26:23.011` and `id=2` created `21:26:23.062`, both `job_type=migrate-one-sbom`, each with `total_items=577`.

**Effect**: 1,144 `migrate-one-sbom` jobs (verified) → 1,144 distinct workflow_ids → exactly 2 workflows per `legacy_sbom_migration_mapping` row.

**Amplifier**: `migrate-one-sbom` has no `serialize_on` clause and no `SELECT workflow_id FROM legacy_sbom_migration_mapping WHERE id=:row_id FOR UPDATE` check at the start of its handler. Both workflows for the same row get fully dispatched.

### 3.2 No Abort Path in Duplicate Workflows

Even with the duplicate dispatch, the downstream steps have idempotent fast paths:

| Step | Idempotency mechanism | Aborts workflow? |
|---|---|---|
| `add-sbom-asset` | `resolve_existing_artifact(dedup_key)` returns existing `artifact_uuid` | **No** — returns success |
| `vulnerability-scan-artifact` | Anti-join on `PackageVulnerability` + `EmptyScan` returns empty | **No** — returns success |
| `ensure-app-version-policy-evaluation` | `try_claim_policy_evaluation` returns COMPLETE | **No** — returns success |
| `artifact-policy-evaluation` | None — re-runs the executable | **No** |
| `activate-asset` | None — `add_asset` with same `(account, app_version, name)` | **Dies** with `AssetNameConflict` |

The unique constraint at activate-asset is the only thing catching the duplicate. 569 of 569 dead `activate-asset` jobs in the nightly run had `attempt_count=1` (no retries) and `failure_reason='Asset with name <X> already exists in this version'`. By the time the loser's `activate-asset` runs, the winner's asset row has typically been in the database for 3+ minutes (p50 gap = 195 s, max gap = 1712 s).

### 3.3 Inefficient Per-Artifact Policy Evaluation

`artifact-policy-evaluation` has no cache. Every invocation:

1. Looks up the parent `PolicyEvaluation` row (`policy_dao.get_policy_evaluation_if_latest`).
2. Constructs a `FindingsBatchWriter` with current policy identity.
3. Calls `executable.execute(reference, context)`, which iterates `app_version_vuln_report(_account, _version, _artifact_uuid)` — the full set of `(vuln, package, distro)` tuples for the artifact.
4. Each tuple is fed through the gates; matching rules fire findings.
5. Findings + links are upserted.

If the same artifact has been evaluated under the same `(policy, digest)` before — say, after a duplicate workflow run, or because the artifact is shared between two app_versions — the work is fully redundant. The upserts collapse (content-addressed by `finding_key`), but the CPU + DB roundtrip cost is paid every time.

For the duplicate-workflow case in nightly, every successful "winner" workflow's `artifact-policy-evaluation` ran the full executable, then the "loser" workflow's `artifact-policy-evaluation` ran the full executable again 2-3 minutes later, producing zero new finding rows.

### 3.4 Metadata-Only Feed Updates Don't Re-Evaluate

Today's feed update workflow drives policy re-evaluation only off the `packages` list from `grype db diff` — the set of `(ecosystem, name, cpe)` entries whose matching layer changed.

The `vulnerabilities` list from the diff (`{added, modified, removed}` keyed by vuln_id) drives the metadata loader (`load_metadata`) but does NOT feed into `enqueue-stale-policy-evaluations`.

**Consequence**: changes that affect policy decisions but don't alter the (package, vuln) matching surface are silent:

- **KEV addition**: CVE-X being added to CISA's Known Exploitable Vulnerabilities catalog. The grype matching layer didn't change (same packages still match same CVE). The new `component_catalog_kev_by_vuln` row gets inserted. Existing findings remain stamped with their old action. A "fail on KEV-listed" rule that was previously not firing for CVE-X never gets re-evaluated until something else drags the affected app_version through evaluation.

- **Severity escalation**: CVE-X's severity is upgraded from Medium to Critical. `component_catalog_vulnerabilities` row is updated by `load_metadata`. Existing findings keep their stamped `warn` action. A rule with severity threshold `critical` that should now fire won't.

- **EPSS spike**: An EPSS percentile shift across a threshold. Same pattern.

- **CVSS revision**: NVD republishes a CVE with revised CVSS scoring. Same pattern.

The only path to noticing is incidental — an unrelated event (asset add to the same app_version, policy edit, app's `active_policy_id` change, or another vulnerability in the same `packages` list happening to share an app_version) drags the app_version through `ensure-app-version-policy-evaluation`, which re-streams `app_version_vuln_report`, which re-reads the metadata tables.

### 3.5 `TRUNCATE + INSERT` Prevents Diff Detection

`apply_metadata_update.sql` uses TRUNCATE + bulk INSERT for the metadata-denorm tables most relevant to policy decisions:

| Table | Operation | Line |
|---|---|---|
| `component_catalog_epss` | TRUNCATE + INSERT | 97 |
| `component_catalog_kev` | TRUNCATE + INSERT | 209 |
| `component_catalog_kev_by_vuln` | TRUNCATE + INSERT | 235 |
| `component_catalog_epss_by_vuln` | TRUNCATE + INSERT | 271 |
| `component_catalog_vuln_score` | TRUNCATE + INSERT | 334 |

Comment in the SQL at line 331: *"TRUNCATE + bulk INSERT: roughly a third of vulns shift between"* — the design justifies the choice on churn rate, but it forecloses any ability to compute deltas. The prior state is gone in the same transaction that loads the new state.

Tables that DO use merge patterns (`component_catalog_providers`, `component_catalog_cvss_sources`, `component_catalog_vulnerabilities`, `component_catalog_vuln_cvss`) use `INSERT ... ON CONFLICT DO UPDATE`, and could be diffed today.

### 3.6 `policy_digest` Stamping on Link Rows

`PolicyFindingLink.policy_digest` is part of every link row. The read query filters by `(policy_id, policy_digest)`. When a policy bundle is edited — even cosmetically (whitespace, comments, rule reordering) — the digest changes.

Consequences:

- All existing link rows still carry the old digest.
- Reads filtered by the new digest return zero.
- Re-evaluation under the new digest writes new link rows duplicating the old ones (one set per digest).

Even if the underlying rule set didn't change (so `finding_key` collisions perfectly upsert the finding rows), the link table doubles. A non-functional edit forces the same effort as a full rule change.

### 3.7 Cosmetic Issues in `ensure-app-version-policy-evaluation`

The cache `(app_version_uuid, policy_id, policy_digest)` works correctly for what it stores, but during the SBOM import workflow it stores a vacuous "COMPLETE" — the new artifact isn't yet linked to the app_version (activate-asset is step 5), so the full sweep over `app_version_vuln_report(account, version, NULL)` returns zero rows. The row is marked COMPLETE with zero findings written.

Subsequent asset adds hit the cache and return immediately. This is correct for the asset-add path because the actual findings work happens in `artifact-policy-evaluation` (step 4 with `_artifact_uuid IS NOT NULL`), and the read-time UNION through `Assets` picks them up. But it means the ensure-step is decorative on this path.

### 3.8 Compounding Failures During Migration

The nightly run amplified all of the above:

- 3 of 6 CC pods crashed mid-run (worker_dead events at 21:40, 21:53, 21:54 with 51 workers killed total)
- ~51 in-flight jobs failed at crash time
- Only one scanner worker was active for the first 5 minutes of scanning (worker `556ff34b` did 622 of the first 700-ish scan jobs)
- `process-feed-update` non-winner jobs took 8-14 minutes due to lock-then-bootstrap-then-check ordering (`feed_update_workflow.py:188` acquires the exclusive phase lock before the skip check at `:196-198`)
- The CardinalityViolation in `findings_writer.add()` (`findings_writer.py:152-162` doesn't dedup by `finding_key`) killed Spaghetti Code's and Backend's policy step

---

## 4. Proposed Reactive Redesign

### 4.1 New Cache Layers

#### 4.1.1 `artifact_uuid → set(policy_id)`

A small materialized table tracking which policies are active on each artifact via its app_version membership.

```sql
CREATE TABLE artifact_active_policies (
    artifact_uuid UUID NOT NULL,
    policy_id VARCHAR NOT NULL,
    PRIMARY KEY (artifact_uuid, policy_id)
);

CREATE INDEX ix_artifact_active_policies_policy ON artifact_active_policies (policy_id);
```

**Cardinality**: bounded by `Assets` count × distinct active policies per artifact (typically 1-2 per artifact in practice).

**Maintenance events**:
- `activate-asset` → INSERT `(artifact_uuid, policy_id)` (idempotent on PK).
- Asset removal → DELETE the row if no other Assets row for that `(artifact, app)` policy combination remains. Use refcount via a count-of-Assets-pointing-at-this-pair check.
- App's `active_policy_id` changes from P1 to P2 → for every artifact in every Assets row of every app_version of this app: DELETE `(artifact, P1)`, INSERT `(artifact, P2)`. Heavy event, but rare.
- App deleted → cascade through assets.

#### 4.1.2 `policy_id → set(rule_digest)`

A small table mapping each policy to its current rule set.

```sql
CREATE TABLE policy_active_rules (
    policy_id VARCHAR PRIMARY KEY,
    policy_digest VARCHAR NOT NULL,     -- metadata, not part of the key
    rule_digests JSONB NOT NULL,        -- set of rule_digest strings
    updated_at TIMESTAMPTZ NOT NULL
);
```

**Cardinality**: one row per policy. Tens to low hundreds of rows even at large scale.

**Maintenance events**:
- Policy bundle uploaded for `policy_id` → recompute the rule set, UPDATE the row in place. Capture the diff (`added`, `removed`) as part of the update.

The digest lives on the row as metadata for cache-staleness detection but is NEVER part of any key. A policy edit that doesn't change any rule's serialized form leaves `rule_digests` unchanged.

#### 4.1.3 In-Process Compiled Rule Set Cache

Per-process `policy_id → ExecutablePolicy` cache (or `rule_digest → CompiledRule` if compiling per-rule), populated lazily and invalidated on `policy_active_rules` updates.

### 4.2 Refactored Link Model

```sql
ALTER TABLE component_catalog_policy_finding_links
    DROP COLUMN policy_digest,
    ADD COLUMN rule_digest VARCHAR NOT NULL;

CREATE INDEX ix_policy_finding_links_rule ON component_catalog_policy_finding_links (rule_digest);
```

**Read path** for "findings of app_version V under policy P":

```sql
WITH active_rules AS (
    SELECT jsonb_array_elements_text(rule_digests) AS rule_digest
    FROM policy_active_rules WHERE policy_id = :policy_id
)
SELECT f.*
FROM policy_findings f
JOIN policy_finding_links l ON l.finding_uuid = f.uuid
JOIN active_rules ar ON ar.rule_digest = l.rule_digest
LEFT JOIN assets a ON a.artifact_uuid::text = l.object_id AND a.app_version_uuid = :app_version_uuid
LEFT JOIN policy_finding_links l_av ON l_av.finding_uuid = f.uuid AND l_av.link_type = 'app_version' AND l_av.object_id = :app_version_uuid::text
WHERE (l.link_type = 'artifact' AND a.uuid IS NOT NULL)
   OR (l.link_type = 'app_version' AND l_av.id IS NOT NULL);
```

Substantively the same UNION shape as today, but joined through `policy_active_rules` instead of filtering by `policy_digest` on the link.

**Effect of policy edits**:
- No rule changes → no link rows touched.
- Rules added → new findings + new links written for each (rule, vuln_tuple) firing.
- Rules removed → links remain; they're just no longer in `policy_active_rules.rule_digests` for that policy_id. If no other policy references the rule_digest, it becomes a candidate for GC.

### 4.3 Mutation Event Catalog

#### Events that trigger evaluation work

1. **New `PackageVulnerability` row** (scanner output):
   - Lookup: `pm → artifacts via artifact_packages` (indexed query).
   - Union the `set(policy_id)` from `artifact_active_policies` for each artifact.
   - Resolve `policy_id → rule_digests` from `policy_active_rules`, dedup the union.
   - Compile cache (`rule_digest → CompiledRule`).
   - Evaluate the rule set against the single `(pm, vuln, distro)`.
   - For each firing rule: upsert `PolicyFinding` by `finding_key`; write vuln link; write artifact links for each artifact containing this pm.

2. **Vulnerability metadata change** (KEV add, severity, CVSS, EPSS):
   - Queue: `vuln_metadata_changes (vulnerability_id, fields_changed)`.
   - Consumer: for each `vulnerability_id`, lookup affected `(pm, distro)` tuples via `PackageVulnerability`, then take the same path as a new `PackageVulnerability` row for each tuple.
   - Same upsert behavior — bit-identical findings are no-ops; changed findings update.

3. **Policy edit (new bundle for `policy_id`)**:
   - Compute new `rule_digests`. Diff against old: `added`, `removed`.
   - For `added`: for each rule, lookup the set of artifacts where this `policy_id` is active via `artifact_active_policies` reverse index, then the set of `(pm, vuln, distro)` tuples in those artifacts. Evaluate the new rule against each tuple.
   - For `removed`: no immediate work. Reads naturally exclude the now-absent rule_digests via `policy_active_rules`.
   - UPDATE `policy_active_rules` in place atomically with the work above. Refcount on `rule_digests` allows GC of orphaned link rows.

4. **App's `active_policy_id` changes** (P1 → P2):
   - For each artifact in the app's assets:
     - DELETE `(artifact, P1)` from `artifact_active_policies`.
     - INSERT `(artifact, P2)`.
   - New rules to evaluate against the artifact's tuples: `policies(P2).rules - policies(P1).rules`.
   - Old links via P1 stay (other apps might still use P1); the app's reads now filter through P2's rule set.

5. **Asset added** (`activate-asset`):
   - INSERT `(artifact_uuid, app's active_policy_id)` into `artifact_active_policies` (idempotent on PK).
   - For each `(pm, vuln, distro)` in the artifact (via `PackageVulnerability` join on `pm` set from `artifact_packages`): evaluate against the new policy's rules ONLY IF no prior finding exists for the rule on this tuple (cache check on existing links).
   - The natural reactive path described in event 1 already covers per-tuple eval; here it's amortized across the artifact's tuples.

#### Events that trigger GC only

6. **Asset removed**: DELETE `(artifact, policy_id)` from `artifact_active_policies`. If the artifact has no remaining policies, its links can be GC'd.
7. **App / app_version deleted**: cascade asset removals.
8. **`PackageVulnerability` deleted** (vuln retraction): the vulnerability link for that tuple becomes orphaned; refcount-decrement the finding.
9. **Policy deleted**: equivalent to "active_policy_id change" for every app using it.

#### Events that require no work

- Policy uploaded (no app references it yet)
- App / app_version created with no assets
- Artifact created without being linked into any asset

### 4.4 Metadata Loader Changes

Replace TRUNCATE + INSERT with merge + RETURNING for the operationally-significant tables.

```sql
-- Example for component_catalog_kev_by_vuln
WITH
removed AS (
    DELETE FROM component_catalog_kev_by_vuln k
    WHERE NOT EXISTS (
        SELECT 1 FROM _tmp_kev_by_vuln t WHERE t.vulnerability_id = k.vulnerability_id
    )
    RETURNING vulnerability_id, 'kev_removed' AS change
),
upserted AS (
    INSERT INTO component_catalog_kev_by_vuln (vulnerability_id)
    SELECT vulnerability_id FROM _tmp_kev_by_vuln
    ON CONFLICT (vulnerability_id) DO NOTHING
    RETURNING vulnerability_id, 'kev_added' AS change
)
INSERT INTO vuln_metadata_changes (vulnerability_id, change_type, change_timestamp)
SELECT vulnerability_id, change, now() FROM removed
UNION ALL SELECT vulnerability_id, change, now() FROM upserted;
```

Similar patterns for `epss_by_vuln`, `vuln_score`, and any other denorm table where the policy gates read derived fields. The `vuln_metadata_changes` queue is the input to event #2 in §4.3.

For fields that update existing rows (severity, CVSS): `INSERT ... ON CONFLICT DO UPDATE WHERE excluded.<field> IS DISTINCT FROM <field> RETURNING vulnerability_id, <field> AS change`.

### 4.5 Diagram: Reactive Event Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              MUTATION SOURCES                                │
└─────────────────────────────────────────────────────────────────────────────┘

  SCANNER WRITES                FEED-UPDATE LOADER          OPERATOR ACTIONS
  ──────────────                ─────────────────           ────────────────
  PackageVulnerability          vuln_metadata_changes       policy upload/edit
  (pm, vuln, distro)            (KEV/EPSS/severity/CVSS)    app active_policy ↔
                                                            asset add/remove
            │                            │                          │
            ▼                            ▼                          ▼
  ┌────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
  │ Per-tuple          │    │ Per-vuln_id          │    │ Targeted             │
  │ evaluation event   │    │ → tuples affected    │    │ re-evaluation        │
  └────────────────────┘    └──────────────────────┘    └──────────────────────┘
            │                            │                          │
            └──────────────┬─────────────┴──────────────────────────┘
                           ▼
            ┌────────────────────────────────────┐
            │ pm → artifacts via artifact_packages│
            │ (indexed lookup)                    │
            └────────────────────────────────────┘
                           │
                           ▼
            ┌────────────────────────────────────┐
            │ artifacts → set(policy_id)          │
            │ via artifact_active_policies        │
            └────────────────────────────────────┘
                           │
                           ▼
            ┌────────────────────────────────────┐
            │ policy_id → set(rule_digest)        │
            │ via policy_active_rules              │
            └────────────────────────────────────┘
                           │
                           ▼
            ┌────────────────────────────────────┐
            │ Compile / lookup rules               │
            │ Evaluate against (pm, vuln, distro) │
            └────────────────────────────────────┘
                           │
                           ▼
            ┌────────────────────────────────────┐
            │ Upsert PolicyFinding by finding_key │
            │ Insert PolicyFindingLink rows       │
            │   (vulnerability + per-artifact)    │
            └────────────────────────────────────┘
```

### 4.6 Capacity Analysis

**Steady-state cost per event**:

| Event | Lookups | Evaluations |
|---|---|---|
| New `PackageVulnerability` | 1 (pm → artifacts) + N (artifact → policies) | |active rules for the tuple| × O(rule eval) |
| Vuln metadata change (single vuln) | 1 (vuln → tuples via PackageVulnerability) + per-tuple cost above | per-tuple |
| Policy edit (new rule R) | 1 (policy_id → artifacts via reverse index) + per-artifact cost | \|R\| × \|tuples relevant to policy\| |
| App active_policy change | \|artifacts in app\| (cache updates) + cost equivalent to policy edit for the rule delta | depends on rule set sizes |
| Asset add | 1 (artifact's tuples via artifact_packages) + per-tuple eval against new policy | per-tuple |

**Cache sizes**:

- `artifact_active_policies`: bounded by `Assets` count × distinct policies per artifact. At 10K assets with typically 1-2 policies each: ~10-20K rows. Each row is small (UUID + varchar).
- `policy_active_rules`: bounded by policy count, typically <100 rows.
- Compiled rule cache: tens to low hundreds of compiled rules in memory per process.

**Worst-case workloads**:

- High-churn feed: large `modified` set in the diff drives many `vuln_metadata_changes` events. Each event is O(tuples × rules), bounded by tuples actually affected.
- Policy edit adding K new rules to a policy active on many artifacts: cost = K × tuples_in_those_artifacts. Acceptable when K is small (typical edit); pathological if edit is rewrite-from-scratch.
- App's active_policy changes on a large app: cost = |artifacts in app| × cache rewrites + per-artifact rule-delta eval. Operator-initiated; rare.

### 4.7 Compared to Today's Costs

| Path | Today | Proposed |
|---|---|---|
| Asset add (new content) | 5-step workflow per asset; artifact-policy re-runs the executable over all artifact tuples | Cache lookup + eval only of tuples × delta-rules |
| Asset add (dedup content already in NG) | 5-step workflow runs end-to-end; artifact-policy re-runs the executable; activate creates a new asset row | Same end state; reactive path has no extra rule-eval work after first artifact |
| Feed update (matching change) | `enqueue-stale-policy-evaluations` triggers `ensure-app-version-policy-evaluation` per affected app_version; each does a full SQL stream | Per-tuple evaluation against active rules; bounded by changed tuples × rules |
| Feed update (metadata-only KEV/severity) | **Silent — no re-evaluation** | `vuln_metadata_changes` queue drives per-tuple re-eval |
| Cosmetic policy edit (whitespace, comments) | Full link-table rewrite under new policy_digest | No-op (diff is empty) |
| Add one rule to a policy in use | Re-evaluate whole policy against whole app_version for every affected app_version | One rule × tuples relevant to this policy_id |

---

## 5. Migration / Implementation Notes

Order of operations to land this without disrupting the existing system:

1. **Land `policy_active_rules` and `artifact_active_policies` as read-only caches first.** Backfill from current state. Maintain via triggers or hooks on the relevant write paths. No reads use them yet.

2. **Switch the metadata loader to merge + RETURNING.** Land `vuln_metadata_changes` queue. Consumer is initially a no-op (just logs). Validates that the deltas are accurate.

3. **Add `rule_digest` to `PolicyFindingLink` alongside `policy_digest`.** Backfill `rule_digest` for existing rows by deriving from the policy bundle for each `(policy_id, policy_digest)`. Both columns coexist.

4. **Switch the read path to use `policy_active_rules` UNION joined through `rule_digest`.** Old reads still work because `policy_digest` is still present and correct. New reads exercise the cache.

5. **Drive new evaluations through the reactive path** for new asset adds. The existing ensure/policy/activate workflow continues to work for old paths; new paths just don't enter it.

6. **Drop `policy_digest` from `PolicyFindingLink`** once all reads have moved.

7. **Retire `artifact-policy-evaluation` as a workflow step.** Per-tuple reactive evaluation has replaced its work. The workflow shortens to: decompose → scan → activate.

8. **Retire `enqueue-stale-policy-evaluations` in favor of the `vuln_metadata_changes` consumer.** The feed-update workflow shortens to: process-feed-update → vulnerability-scan-batch → emit-vulnerability-events.

Backfill the caches in step 1 via a one-time job that walks the existing assets and policies. The cache reads should match the existing schema's read query results within the same transaction.

---

## 6. Open Questions

1. **Per-rule compilation vs per-policy compilation.** If two policies share a rule, do we compile it once and dispatch by reference, or compile it once per policy? In-process cache keyed by `rule_digest` is the cleanest, but requires the `ExecutablePolicy` builder to break apart into per-rule compilable units. Current `build_policy` is monolithic.

2. **Allowlist scope and invalidation.** Allowlists are currently stamped on link rows scoped to `(policy_id, policy_digest)`. Under the redesign, allowlist application could be:
   - Stamped at write time on link rows (current model, but rule_digest-scoped) — simpler, but allowlist edits force link refresh.
   - Applied at read time via a separate `allowlist_active_rules` mapping — costlier reads but free allowlist updates.

3. **Worker pool sizing under reactive load.** The per-tuple reactive path produces many small jobs. The current pool sizing (`vuln_scanner` pool_size=1 per pod) was tuned for batched scan units. Per-event handlers might fit on the general `worker_pool` or warrant a new pool with different sizing.

4. **Backfill cost.** The one-time backfill of `artifact_active_policies` for an existing environment with N assets is N rows. The backfill of `policy_active_rules` is M rows for M policies. Both small. The backfill of `rule_digest` on existing `PolicyFindingLink` rows is the heavy step — proportional to the size of the links table.

5. **Feed-update workflow consolidation.** Today's feed-update workflow has four steps. Under the redesign, steps 3 and 4 (emit events, enqueue-stale-policy-evaluations) become redundant with the `vuln_metadata_changes` consumer. Whether to keep them as transition compatibility or remove them outright depends on how aggressively the redesign lands.

6. **Concurrency model for the reactive consumer(s).** Per-tuple events arrive at potentially high rate during feed updates. Batching strategy (process N events per consumer invocation) and shard key (by `pm_id`? by `(policy_id, pm_id)`?) determines steady-state throughput. The current job framework is per-job; could either submit one consumer job per event (overhead-heavy) or batch.

7. **Audit trail.** Today's `PolicyEvaluation` rows track an "evaluation epoch" per `(app_version, policy, digest)` with status and `evaluated_at`. Under the reactive model, the notion of an epoch fades — there's no full evaluation, just ongoing per-event work. UI surfaces that show "last evaluated at" for an app_version would need to be reframed (e.g., last_metadata_change_at, last_relevant_event_at).

---

## 7. Summary

The data model is sound. The execution layer needs to stop pretending it's a batch-evaluation system and start operating as an event-driven cache-maintenance system, where the scanner's output is the authoritative event stream and policy decisions ride downstream of it.

The redesign keeps `PolicyFinding` content-addressed and shareable across policies (which today's design already supports), introduces `artifact_active_policies` and `policy_active_rules` as the only new caches, refactors `PolicyFindingLink` to carry `rule_digest` instead of `policy_digest`, and makes the metadata loader emit deltas instead of destroying prior state.

Net effect: the duplication of work that the system currently absorbs via content-addressed dedup is moved upstream, to the decision of whether to do the work at all. Asset adds become near-O(1) when content is shared. Feed updates with metadata-only changes propagate to existing findings. Cosmetic policy edits cost nothing. Real changes — new rules, new vulnerabilities, content additions — pay only their actual marginal cost.
