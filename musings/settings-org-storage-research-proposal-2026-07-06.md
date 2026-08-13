# Settings storage & org-scoping — research proposal

Status: draft, 2026-07-06. Author: host session host-0629-112824.
Purpose: replace hand-wavy "scopeless" language with the exact storage mechanism,
quantify the current state, and propose a forward+backward remediation.

## 1. The exact mechanism (verified from code + disk, not inferred)

**Where settings live.** A setting is a row in a `settings` table. That table
exists once per organization, in a separate SQLite file: `data/orgs/<slug>.db`.
On disk today:

| file | settings rows |
|---|---|
| data/orgs/autonomy.db | 693 |
| data/orgs/personal.db | 452 |
| data/orgs/anchore.db | 137 |
| data/orgs/blindhash.db | 125 |
| data/orgs/jira.db | 119 |
| data/graph.db (legacy default) | 116 |

`settings` columns: `id, set_id, schema_revision, key, payload, publication_state,
supersedes, excludes, deprecated, successor_id, created_at, updated_at, expires_at`.

**What "scopeless" actually is.** There is *no* separate scopeless database.
`resolve_caller_db_path(org)` (tools/graph/db.py:67) does literally:

```python
slug = org or "personal"
org_path = data/orgs/<slug>.db
return org_path if org_path.exists() else DEFAULT_DB   # DEFAULT_DB = data/graph.db
```

So `org=None` (and `org=""` — any falsy value) resolves to **`data/orgs/personal.db`**.
The code comment calls this "scopeless convergence (auto-txg5.3): every write without
an explicit org lands in the operator's personal DB." "Scopeless" = "no org was
passed" = **defaults to personal.db**. Empirically confirmed: the settings I kept
calling scopeless (e.g. `dashboard.feature_flags/voice.reset_suppression`) physically
live in `data/orgs/personal.db` (20 feature-flag rows, 4 `voice.reset_suppression`
versions), and are absent from autonomy.db.

**Two different "default org" resolvers exist, and they disagree when unset:**

| resolver | code | default when `GRAPH_ORG` unset | file it hits |
|---|---|---|---|
| `settings_ops.CALLER_ORG` | contextvar → `GRAPH_ORG` env → `None` | `None` → `"personal"` | data/orgs/personal.db |
| `_credentials_org()` (credentials + session_launcher) | `GRAPH_ORG or GRAPH_SCOPE or "autonomy"` | `"autonomy"` | data/orgs/autonomy.db |

They agree only when `GRAPH_ORG` is explicitly set. It is set for **container
sessions** (`agents/session_launcher.py:983`, `-e GRAPH_ORG=<slug>`). It is **not
set** for the dashboard process or the host terminal. So:

- Dashboard process + host terminal: settings default to **personal.db**.
- Credential pollers (in the same dashboard process): default to **autonomy.db**.
- Containers: both resolvers agree on the container's `GRAPH_ORG`.

That single disagreement is the entire root of the "settings written to the wrong
place" class of bug, including the Claude-credentials failure on 2026-07: the install
write path used `CALLER_ORG` (→ personal.db from the host terminal) while the poller
reads via `_credentials_org()` (→ autonomy.db).

## 2. What is actually in personal.db (the "scopeless" pile), quantified

452 rows across 15 set_ids:

| rows | set_id (prefix) | nature |
|---|---|---|
| 244 | dashboard.participant.activity | ephemeral session-activity telemetry |
| 119 | autonomy.schema* | the schema registry (global schema definitions) |
| 32 | dashboard.operator.activity | operator activity telemetry |
| 20 | dashboard.feature_flags | live config (7 distinct flags incl. voice.*) |
| 14 | autonomy.workspace | workspace configs |
| 10 | dashboard.session.upload | upload records |
| 3 | dashboard.voice.transcription | live voice config |
| 3 | dashboard.action-registry-state | coordinator action state |
| 1 each | autonomy.org, coordinator-canvas, coordinator-tile, … | misc dashboard state |

This is **not orphaned data.** The dashboard reads personal.db on essentially every
request (its `CALLER_ORG` resolves there), so this is its *live* store. It is
internally consistent — the breakage is only *cross-resolver* (credentials).

## 3. How many code paths

`CALLER_ORG` appears **482 times** across tools/ (includes tests). It is not a
handful of misuses — it is *the* standard settings-access pattern, used pervasively.
Its correctness depends entirely on `GRAPH_ORG` being present in the process
environment. There is already a checker, `tools/graph/checks/settings_request_org.py`
(21 refs), implying prior awareness of an org-passing hazard — its exact contract is
an open question (see §5).

## 4. When is defaulting to personal.db appropriate — or is it ever?

- **Genuinely per-operator data**: appropriate. personal.db is meant to be the
  operator's own store.
- **The schema registry (119 rows)**: questionable. Schemas are global; housing them
  in one operator's personal DB is wrong for any multi-operator future.
- **Dashboard config + telemetry (~300 rows)**: works for a single-tenant
  deployment, but semantically this is "the autonomy deployment's state," not "an
  operator's personal settings." It is misfiled the moment there's more than one
  operator or a real per-org boundary.
- **The core hazard**: personal-as-default is *silent*. Any code that forgets to pass
  an org, or runs without `GRAPH_ORG`, writes to personal.db and nothing complains —
  it only surfaces when a *different* resolver reads the same data. So "scopeless is
  fine" holds **only** when the identical default is used for both the read and the
  write of a given datum, which is not guaranteed across subsystems.

## 5. Open research questions (what this proposal is asking to fund)

- **Q1 — Deployment-org policy.** Should the dashboard's operational settings live in
  personal.db or in the deployment's own org (autonomy.db)? This is a decision, not a
  discovery; everything downstream depends on it.
- **Q2 — Schema registry home.** Should `autonomy.schema*` be in personal.db, or a
  shared/global store? Multi-operator correctness.
- **Q3 — Exhaustive code-path audit.** Enumerate every production (non-test) call site
  that reads/writes settings; classify each by resolver (`CALLER_ORG` vs
  `_credentials_org` vs explicit) and the org it lands in. 482 refs → filter tests,
  categorize the rest.
- **Q4 — Other read/write splits.** Credentials is one datum written via one resolver
  and read via another. Are there others? (Same-shape latent bugs.)
- **Q5 — Existing guard.** What does `settings_request_org.py` enforce, and can it be
  extended to fail on silent personal-default writes in production?
- **Q6 — Graph notes/sources.** Notes and sources are a *separate* system from
  settings. Are they org-scoped too, and is any note data misfiled? (Not yet
  investigated — explicitly out of scope of the facts above.)

## 6. Proposed remediation

**Forward (stop new divergence):**
1. Unify the credentials resolver: make `claude_cmd`'s read+write use one resolver
   (align on `_credentials_org` = autonomy, since the data is there). Small, local fix.
2. Resolve Q1; if the answer is "autonomy," set `GRAPH_ORG=autonomy` on the dashboard
   **and** host terminal — but only *after* §backward migration, never before.
3. Extend the org-passing checker to warn/fail on production org-less writes.

**Backward (reconcile existing data) — strict ordering to avoid an outage:**
4. Per-set_id disposition: schema → shared; credentials-adjacent → autonomy;
   genuinely-personal → personal; pure telemetry → possibly purge.
5. For each datum that must move: **copy to the new org first** (data now in both),
   **verify the consumer reads it through its own resolver**, then **remove the old**.
   Never flip a reader's org before its data exists in the new location.
6. Dedupe anything present in both personal.db and autonomy.db.

## 7. Non-negotiable sequencing risk

The dashboard depends on reading personal.db *right now*. A premature
`GRAPH_ORG=autonomy` flip on the dashboard would strand 452 rows (flags, voice,
schemas, coordinator state) and take it down. Migration must be additive-first,
reader-flip-last, per §6.

---

# Part 2 — grounded in the vision (added after reading the design notes)

## 8. The intended model (three orthogonal axes)

From the three-axis model (`graph://8cf067e3-ca3`), the per-org-DB decision
(`graph://7c296600-19b`), and the Setting Primitive signpost (`graph://0d3f750f-f9c`):

1. **Physical org isolation** — each org has its own `data/orgs/<org>.db`. Rule:
   *"Settings live in the org-DB whose entity they describe."* The **personal** org
   is the operator's sovereign private graph. CORRECTION (2026-08-13): the earlier
   claim here — "never a federation peer, never shared at any publication_state" —
   is WRONG, and verified so in code: `personal` IS a default read peer
   (`cross_org.list_org_slugs` globs it in), and its `published`/`canonical` rows
   ARE read-through-able cross-org — which is *intended* (e.g. a published personal
   identity a peer must verify). The real personal invariant is on axis 3 (**sync**),
   not read: personal content never *synchronizes* onto a **different user's**
   machine, at any publication_state — but it *does* synchronize across the
   operator's **own fleet**. Read-through ≠ sync. See the rubric `graph://4d88c2ad-625`.
2. **`publication_state`** (`raw → curated → published → canonical`) — the
   visibility/maturity axis. NOT access control, NOT propagation. The rule that
   matters: **the public surface of an org DB = rows where `publication_state IN
   ('published','canonical')`; that set IS the cross-org / other-user view.** So
   `raw`+`curated` are local to the org DB; `published`+`canonical` are the shared
   surface that federation/multi-user sync will propagate.
3. **Propagation / sync** — how one org's published surface reaches another
   operator's mirror. Explicitly deferred; keys on axes 1+2 so no retro-migration.

## 9. THE POLICY — which isolation method, when

There are two ways to keep a value local, and they answer *different* questions:

- **Personal org (`data/orgs/personal.db`)** = local **by ownership, permanently.**
  Use when the value is the *operator-as-individual's* — personal preferences,
  personal/host-machine-specific state, individual secrets — such that it must
  **never** be shared even with org teammates. `publication_state` is moot here;
  personal is never federated.
- **Org DB (e.g. `autonomy.db`) + `publication_state='raw'`** = local **by maturity,
  provisionally.** Use when the value is owned by the *org/deployment/platform* but
  you're keeping it local for now. Promote to `published`/`canonical` when you want
  it to sync to teammates (multi-user) or subscriber orgs (cross-org).

**Decision rule:** *Whose is this?* If it's mine-the-person forever → personal org.
If it's the org's (the deployment's / the team's) → the org's DB, at `raw` until you
choose to share it. **The anti-pattern is using personal.db as a generic "local
default"** — that misfiles org-owned config as operator-private, and it will *never*
sync when multi-user lands. That anti-pattern is exactly what the `org=None →
personal.db` default produces for any process without `GRAPH_ORG`.

## 10. Completed inventory — what is misfiled vs correct

Same set_ids appear in BOTH DBs because container writes (`GRAPH_ORG=autonomy`) land
in autonomy.db while dashboard-process writes (no `GRAPH_ORG`) land in personal.db:

| set_id | personal.db | autonomy.db | correct home (per §9) |
|---|---|---|---|
| autonomy.schema* | canonical 119 | canonical 119 | **autonomy**, published/canonical (platform publishes schemas to subscriber orgs). **Duplicated — dedupe.** |
| dashboard.feature_flags | canonical 19 / raw 1 | raw 2 | **autonomy** (deployment config). Split; consolidate to autonomy. |
| dashboard.voice.transcription | canonical 3 | canonical 1 | **autonomy** (deployment config). Split. |
| dashboard.participant.activity | raw 244 | — | **autonomy**, raw (deployment telemetry); arguably purgeable. |
| dashboard.operator.activity | raw 32 | — | **autonomy**, raw. |
| dashboard.session.upload | raw 10 | raw 46 | **autonomy**, raw. Split. |
| dashboard.coordinator-* | a few | the bulk (raw) | **autonomy** (already mostly correct). |
| autonomy.workspace | canonical 13 | canonical 6 / raw 2 | **autonomy**. Split — personal copy is stale/misfiled. |
| dashboard.claude.credentials (data) | — | raw 2 | **autonomy** — correct. |

Verdict: essentially **nothing** in personal.db is genuinely operator-private. It is
all deployment/org-owned config, telemetry, or the platform schema registry that
landed there by the `org=None` default. Personal.db is currently a mis-scoped mirror
of autonomy-org data, not a store of personal data.

## 11. Note-update plan (what the operator asked for)

The docs never state *when to use personal-org vs org+raw* — that's the gap. Proposed:
1. Add a **"Which isolation axis, when"** section (the §9 policy) to the Setting
   Primitive signpost (`0d3f750f-f9c`) and cross-link it from the three-axis note
   (`8cf067e3-ca3`).
2. Fold the existing `None→personal` pitfalls (`dbe2344d-baf`, `96f78083-61a`) into a
   single authoritative pitfall that ends with "…and here's the policy for where it
   *should* have gone" → links to §9.
3. Correct my own rule-note (`ef45a53b-5d1`): the durable fix is not per-write
   vigilance but (a) unify the credentials resolver, (b) decide the deployment-org
   policy, (c) migrate per §6 additive-first.
Do NOT rewrite the vision notes unilaterally — confirm the §9 policy wording with the
operator first, since "nail down this policy" is a joint decision.
