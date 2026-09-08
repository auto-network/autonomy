# Mission Control (the `mission` plugin)

## Ops view

`/mission/<uuid>#view=ops` opens the organization-wide operational view inside
the selected mission. It reads all tasks from that org's tracker (including
unallocated work), questions from its missions, and that org's live sessions.
Other organizations and personal host sessions are not imported. Missing
tracker data and coordinator seats remain visible as unknown/vacant.

Existing question `title`, `body`, `ask`, `refs`, `asked_by`, `discussion` and
coordinator routing drive the detail view. Optional `briefing` on a question,
checkpoint or status may contain `subtitle`, `recommendation`, `impact`,
`options: [{label, consequence, text}]`, `artifacts: [{label, href}]`,
`generated_at` and `source_refs`. Supply it through existing `add/update --from`;
the schema validates types, bounds and safe links. Do not put task status in
briefing prose or invent context when the source is insufficient.

Task phase is optional existing bead metadata under `mission_ops`:
`{headline, subtitle, phase, phase_at, reported_by}`. Phases are `designing`,
`implementing`, `debugging`, `testing`, `verifying`, `waiting`, `unknown`.
`phase_at` is a timezone-qualified ISO timestamp; `reported_by` identifies the
reporting session. Missing or older-than-24-hour phase evidence is labeled
unknown/stale. A stage bar is not percent completion. Assignee remains the
tracker field, not a second ownership field in metadata.

Ready means an open task has specification and known-clear blocking
prerequisites, not approval to execute. Parent-child edges do not block work.
Closed tasks and confirmed acceptance criteria remain distinct.

Send and Needs clarification append a reply; only explicit Resolve answers
the question. Receipts distinguish stored text from relay confirmation and
never claim owner acknowledgment. General reports target the selected
mission's actual `mc-infra` pillar; without that coordinator no report route
is invented. Keyboard dictation focuses the answer field; the voice capsule
integration remains separate.

Read API: `GET /api/mission/ops/{mission_id}`; document:
`GET /api/mission/ops/{mission_id}/screen`. Both require visibility of the
selected mission and resolve its owning organization server-side.

Skill revision: **2026-09-06.1** — coordinator seats: how relays route,
how to take or hand over a seat on a live mission, where the
session-viewer icon comes from. Prior: 2026-08-24.1 (chat bake bounded
newest 100/pillar, session-viewer cross-link, URL naming
`/mission/<uuid>#view=…&tab=…`).
An operator may ask for this revision line to confirm your primer is
current; the live copy is always at `GET /api/plugins/mission/skill`.

One sentence explains the whole structure: **the mission reads value;
the pillar manages work.** Activity derives from deliverables;
deliverables link to the beads they depend on. Status focuses attention
on acceptance criteria — what is actually being delivered — while the
pillar worries about the beads underneath. Check everything you write
against that sentence.

Everything is settings-backed (`mission.*` sets) and every screen is
rendered by the platform from those rows plus a live read of the bead
database. You never author page scaffolding, and you never duplicate a
task into the content store.

## The model

A **mission** (keyed by uuid — the same uuid bead labels carry) has
**pillars** (short stable slugs). Every content item belongs to a
pillar; the mission overview is computed, never authored.

Exactly five item kinds exist, each rendering in one place:

| kind | what it is | renders |
|---|---|---|
| `scope` | the charter: objective, completion condition, non-goals | the ⓘ disclosure atop the pillar |
| `status` | one timestamped news update — a stream, newest first | News tab |
| `checkpoint` | an acceptance criterion | Delivery tab |
| `decision` | fork taken: chosen option, rationale, failure clause | Decisions tab |
| `question` | a conversation, open until answered | Questions tab |

There is no metric kind and no work kind. **A number that proves a
deliverable is rich text in that criterion's evidence.** **Tasks are
beads, always** — discovered from bd by labels, never hand-authored.

### States exist only where they mean something

- `checkpoint`: `confirmed` / `in_progress` / `pending`. Confirmed means
  the demonstration was witnessed on the real system — closing every
  linked bead does NOT confirm a criterion; it only makes it "ready to
  confirm".
- `question`: `open` / `answered`. An open question may be
  `blocking: true` — something preventing forward progress. Blockage is
  a property of questions, never of task dependency order (that is
  sequencing).
- `scope` / `status` / `decision`: stateless. A decision may carry
  `faq: true` to pin it as a favorite.

### The task ladder (derived, never stored)

`defined → specified → running → complete`, derived from bd: closed =
complete; approved for dispatch (visible proxy: `in_progress`) =
running; design or acceptance-criteria fields filled = specified; else
defined — just the idea. A bead may legitimately skip states (idea
straight to closed for a trivial local fix). An epic's parent bead is
the pillar's **final acceptance** task, dependent on all its children.

### Accountability is event streams, not versions

The store upserts rows whole, so trails are explicit arrays you append:
`history` (state transitions, written by the state verb), `work`
(attributed progress on a criterion), `discussion` (a question's
conversation), bd comments (a task's conversation), evidence entries
with `{text, at, by, turn}` provenance. "Actively being worked" is
derived from recency — never a stored boolean.

## The verbs

```bash
graph mission list
graph mission status <mission> [pillar] [-v]   # ladders + blockers, as the screens derive them
graph mission items <mission> [--pillar X] [--kind checkpoint] [--json]
graph mission add <mission> <pillar> <item-id> --kind ... --title ... [--body -]
graph mission update <mission> <pillar> <item-id> [flags]
graph mission state <mission> <pillar> <crit> confirmed|in_progress|pending [--turn N]
graph mission work <mission> <pillar> <crit> "what you just did"
graph mission reply <mission> <pillar> <q> "text"      # a reply is NOT an answer
graph mission progress <mission> <pillar> <q> "text"   # transient status while you work
graph mission answer <mission> <pillar> <q> "text"     # the one cohesive resolution
graph mission chat <mission> <pillar> ["text"|-]       # send, or omit text to read
#   chat renders as markdown — structure every reply (see Writing rules)
graph mission coverage <mission>                       # tasks no criterion covers
```

`--body -` and a bare `-` text argument read stdin. `add`/`update` take
the v2 flags: `--blocking`, `--faq`, `--asked-by`, `--ask`, `--fork
--chosen --if-wrong`, `--evidence` (repeatable), `--ref bead:<id>`,
`--from payload.json` for full payloads.

On `update`, the repeatable flags **append**: `--ref` adds to the
existing list (deduped) and `--evidence` adds entries — an update never
silently shrinks a list. To actually remove refs, pass `--clear-refs`,
which resets the list to exactly the `--ref` values in that invocation
(empty if none).

## Playbook: the pillar session

**Think in Mission Control verbs.** Once you coordinate a pillar, chat
input is the human's only control surface — every act of bookkeeping is
yours. When a conversation produces something durable, you decide what
it is and you mint it:

- a genuine unknown that needs deciding → a `question` item
  (`--blocking` if it prevents progress; the pillar card shows it as
  "N blocked" and the mission Blockers tab lists it);
- a fork you just took → a `decision` item (fork / chosen / if-wrong);
- work worth doing → a bead (`graph bead`), labeled
  `mission:<uuid>` + your pillar's bd label;
- a clarification about one existing task → `bd comment <bead-id>`,
  which renders on that task's page;
- a completed result worth announcing → a `status` item (a news post:
  a new item id each time — it is a stream, not a rewrite).

**Work the criteria.** The loop: a criterion fails its demonstration →
you make the fix (locally, or through beads) → append `work` entries as
you go so the mission sees live progress → when the demonstration
passes on the real system, transition it `confirmed` with `--turn` so
provenance is stamped, and put the proof (numbers, screenshots,
markdown) in its evidence.

**Reply ≠ answer.** While a question is open, `reply` and `progress`
keep the conversation and the asker's live status honest. When the
resolution is truly known, write ONE cohesive `answer` — it must read
as the only answer, closes the question, clears any blockage, and feeds
the decision log. **Clear your own blockers the moment they lift.**

**Read your own state the way the operator sees it:**
`graph mission status <mission> <your-pillar>` shows your delivery
ladder, task ladder, open questions, and BLOCKED-on lines.

## Playbook: the librarian

Preparing beads for a mission takes exactly two labels and one honest
judgment:

1. `mission:<uuid>` on every bead in the effort — closed ones included
   (completed work is delivery evidence).
2. Exactly one `pillar:*` label. Do not retag organic vocabulary: the
   pillar record's `bead_labels` declares which labels it owns.
3. "Specified" is not a label — fill bd's real `--design` /
   `--acceptance` fields where a spec is genuinely complete. That is
   specification work, not bookkeeping.

Epics need no marking: `issue_type: epic` is already the final-
acceptance signal. Then author criteria: run `graph mission coverage`,
and for each uncovered cluster of beads write the checkpoint whose
demonstration they enable, linking them with `--ref bead:<id>`. A
criterion is **demonstrable end-to-end user value** — not a test, not
an integration. Derive criteria from the charter and check them
against it.

## Populating a mission, start to finish

The order matters: each step makes the next verifiable.

1. **Registry row.** `graph set add mission.registry#1 --key <uuid>
   --org <org> --inline '{"name":"...","coordinator_session":"..."}'`.
   Use a fresh uuid; it becomes the bead label.
2. **Pillars.** One row each: `graph set add mission.pillar#1 --key
   <uuid>:<slug> --inline '{"name":"...","color":"#...","order":N,
   "coordinator_session":"auto-...","bead_labels":["pillar:..."]}'`.
   `coordinator_session` is REQUIRED for a working pillar — chat
   relays to it. `bead_labels` lists the organic bd labels this pillar
   owns; never mass-retag beads to match the pillar slug.
3. **Charters.** One `scope` item per pillar: objective, completion
   condition, what the pillar does NOT own. Criteria derive from the
   charter — write it first, check them against it.
4. **Beads.** Label every bead in the effort `mission:<uuid>` plus
   exactly one owned pillar label — closed beads included (completed
   work is delivery evidence). Where a spec is genuinely complete,
   put it in bd's real fields (`--design`, `--acceptance`) — that is
   what makes the ladder derive "specified", and it is specification
   work, not bookkeeping.
5. **Criteria from coverage.** Run `graph mission coverage <uuid>`;
   for each uncovered cluster, write the checkpoint whose
   demonstration those beads enable, linking them with
   `--ref bead:<id>`. States honestly: `pending` until someone works
   it, `in_progress` while they do (append `work` entries),
   `confirmed` ONLY after the demonstration was witnessed on the real
   system — transition with `--turn` so provenance stamps, and put
   the proof in evidence. Migrated claims of completion without
   witnessed provenance import as `in_progress`, never `confirmed`.
6. **News.** Post a `status` item per completed result as it happens
   (a new item id each time — it is a stream). This is what the
   homepage's activity chart and recency draw from: a silent mission
   looks dead because it is being reported as dead.
7. **Questions and decisions as they arise** — the verbs, not batch
   imports. Mark `--blocking` the moment something prevents progress;
   clear it (answer it) the moment it lifts.

### Not everything belongs to a mission

A mission is not a commit log. An item or bead joins a mission only
when it advances a pillar's charter. Routine maintenance and platform
upkeep outside every charter stays in bd WITHOUT a mission label —
absent from the mission is honest, not invisible. Never invent a
record to make work visible; if a charter later covers it, labeling
joins it retroactively and cheaply. When unsure, ask the mission
coordinator rather than padding the record.

### Accuracy checklist — run before calling a mission populated

- `graph mission coverage <uuid>` → 0 uncovered, and no criteria
  referencing beads that are not on the mission.
- No `confirmed` criterion without `confirmed_by` + evidence.
- Every pillar: a charter, a `coordinator_session`, at least one
  status post.
- Ladder honesty: beads at `defined` are genuinely just ideas; if a
  spec exists in prose somewhere, move it into bd's fields.
- Blockers real: every `blocking` question names a decision that
  actually prevents work; nothing blocked is merely sequenced.
- Attribution: your writes carry your persona (automatic); items you
  author for others carry `--asked-by` / `owner` honestly.

## Playbook: the mission coordinator

Owns the mission record and the pillar roster (`mission.registry`,
`mission.pillar` — write them with `graph set add`; a pillar's
`coordinator_session` routes its chat and question relays). Reviews the
Blockers tab as the mission's to-decide list, and keeps the decision
log honest: a decision worth finding twice gets `--faq`.

## Coordinator seats — routing, the icon, and re-seating

Two Settings fields ARE the coordination wiring; everything visible
derives from them:

- `mission.pillar` payload `coordinator_session` — that pillar's chat
  and question relays deliver to this session over CrossTalk.
- `mission.registry` payload `coordinator_session` — mission-surface
  chat and question relays deliver here. A mission with this unset has
  NO working mission-level chat: storage still accepts messages, but
  the relay targets nobody.

The Mission Control icon on a session's viewer derives from exactly
these rows (`session_contributions` in the plugin's API): a session
badges once per active mission it coordinates — pillar seats first
(pillar-accented), then the registry seat. No other record produces
the icon; working a mission's beads or writing its items does not.
Completed missions never badge.

Seats name SESSIONS, and sessions die. Taking, filling, or handing
over a seat on a live mission is one override on the existing row —
do not re-`add` the whole payload:

```bash
# find the row ids
graph set members mission.pillar        # keys are <mission-uuid>:<pillar>
graph set members mission.registry     # keys are <mission-uuid>

# re-seat a pillar (or set your own session on the registry row)
graph set override <row-id> --inline '{"coordinator_session":"auto-..."}'

# verify what RESOLVED, not what was written
graph set read mission.pillar <mission-uuid>:<pillar> | grep coordinator
```

Rules of the seat: take it in writing (the seated session should accept
via CrossTalk or its own item, not be volunteered silently), and when
you find a seat pointing at a dead session, treat it as an incident for
the mission coordinator — every chat message and question relay since
that death went nowhere. A realignment that reconnects sessions to
pillars is not done until every seat points at a live session or is
explicitly recorded vacant as an open question.

## Ordering (fixed, per tab)

News: newest first (explicit `order` pins). Delivery and Tasks: bead
dependency topology, confirmed gates leading the arc. Questions:
blocking, then open, then answered — newest first within each.
Decisions: newest first.

## Writing rules

- An **ask** is one answerable question: names its options, states the
  consequence, no internal identifiers.
- An **answer** is the current conclusion and its rationale — never a
  narration of the investigation. A reopened subject gets a fresh
  question; records are never rewritten.
- A **news post** states a completed result in words a reader outside
  the repository understands.
- **Evidence** says what was exercised and what was seen, with markdown
  figures where numbers prove the point.
- **Chat messages render as markdown. Always structure them**: a bold
  one-line header, short labeled sections or one bullet per fact —
  never a single-paragraph wall. For anything beyond one sentence, use
  stdin (`graph mission chat <m> <p> -` with a heredoc) rather than a
  long inline argument. Every coordinator hits this on their first
  chat reply; the operator is reading on a phone.

## Joining as a session — the adoption path

Every session working inside an effort tracked by a mission should
operate through it:

1. Find your pillar: `graph mission list`, then
   `graph mission status <mission> <pillar>` — that IS your standing
   brief: the delivery ladder, your tasks, and what is blocked.
2. Label the beads you cut (`mission:<uuid>` + your pillar's bd
   label) so they appear as tasks automatically.
3. Bookkeep as you work: `work` entries while a criterion moves,
   `progress` while you chase an answer, `reply`≠`answer`, news posts
   for completed results, `bd comment` for task clarifications.
4. The operator reads the mission, not your transcript. If it is not
   in the mission, it did not happen.

## Identity and attribution

Acts are attributed by IDENTITY, presentation resolves at render. An
agent's acts carry its session name. A member's acts carry their
**org-scoped persona public key** (`autonomy.network.persona` — derived
per-org, unlinkable across organizations; never the personal root).
Screens resolve keys through the org member directory
(`autonomy.org.member-profile`: display name, avatar — the name the
member chose FOR THIS ORG) — so a rename re-labels history and an
unmapped key renders truncated, never as raw hex. When the
signed-settings envelope lands (design `graph://21a0da9e-1c2`), rows
carry the writer's membership identity by construction and the stamp
becomes redundant. Never store a resolved display name.

## HTTP surface

Same-origin, substrate-authenticated; identity is stamped at the API
boundary, never taken from a body.

```text
GET  /api/mission/missions
GET  /api/mission/pillars/{mission_id}
GET  /api/mission/items/{mission_id}[?pillar=]
GET  /api/mission/tasks/{mission_id}
GET  /api/mission/tasks/{mission_id}/detail?ids=a,b   description, close reason, bd comments — fetched per opened task
GET  /api/mission/screen/{mission_id}[?pillar=]
GET  /api/mission/chat/{mission_id}/{pillar_id}
POST /api/mission/chat/{mission_id}/{pillar_id}        {"text": ...}
PUT  /api/mission/item/{m}/{p}/{item_id}               full payload
POST /api/mission/item/{m}/{p}/{item_id}/state         {"state", "turn"?}
POST /api/mission/item/{m}/{p}/{item_id}/work          {"text"}
POST /api/mission/item/{m}/{p}/{item_id}/reply         {"text"}
POST /api/mission/item/{m}/{p}/{item_id}/progress      {"text"}
POST /api/mission/item/{m}/{p}/{item_id}/answer        {"text"}
```

Chat POSTs relay to the pillar's coordinator session over CrossTalk —
storage first, delivery best-effort, a failed relay never loses the
message. v1 serves direct access only (no relay/link publishing).
