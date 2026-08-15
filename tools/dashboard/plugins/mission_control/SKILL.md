# Mission Control — pushing a mission site from an agent session

Mission Control (`/mission-control`) hosts native sites, one per mission,
with immutable revision history.

A mission is a set of screens and a decision log. §6: a visitor asks a
question, you answer, and the pair becomes the record — reopenable if your
answer was not right. §7: presence, and what changed since someone last
looked. §8: pillars — a mission split into dedicated sub-coordinators, each
with its own screen, presence surface and anchored conversation. §9: how to
write what goes in the record. **§10 is the contract — what you push and
what the platform adds. Read it first if you read nothing else.**

Dashboard base URL: `https://localhost:8080` on host-network sessions,
`https://host.docker.internal:8080` from bridge-network containers (`curl -sk`).

Unlike Present, there is **one store, one obvious content route, and no
separate publish step**. A push both stores the revision and makes it
current, atomically.

## 1. Create a mission (once)

```bash
curl -sk https://host.docker.internal:8080/api/missions \
  -X POST -H 'Content-Type: application/json' \
  -d '{"name": "OSS Insights", "coordinator_session": "'$AUTONOMY_SESSION'"}'
# → 201 {"mission": {"mission_id": "<uuid>", "name": "...", "coordinator_session": "...", "created_at": ..., "current_revision_id": null}}
```

The entity is deliberately minimal — `{mission_id, name, coordinator_session,
created_at}`. `coordinator_session` is plain data, not an invariant: nothing
binds it, and it goes stale if the coordinator session is replaced. The Q&A
relay reads it at message time, so a stale value delivers messages to a
session that is gone. Set it correctly at creation.

## 2. Push a site revision (the only call you need per update)

```bash
curl -sk https://host.docker.internal:8080/api/missions/<mission_id>/site \
  -X POST -H 'Content-Type: application/json' \
  -d '{"html": "<full self-contained HTML>", "note": "rev11: code-verification pass folded in"}'
# → 201 {"revision": {"revision_id": "<uuid>", "mission_id": "...", "revision_seq": 11, "note": "...", "created_at": ..., "byte_size": ...}}
```

`note` is optional but recommended — it's the one-line description that
shows up in the revision history list, and it's free now, annoying to
retrofit later. This call **is** the publish step. There is nothing else to
call afterward.

`note` is a changelog entry, not content — one line, what changed, not the
substance of the change (that belongs in the site's own HTML, where you
already have it). Redundantly duplicating your narrative into `note` just
makes the revision history list unreadable.

- Good: `"rev15: corrected licensing narrative with measured ClearlyDefined/deps.dev evidence"`
- Bad: `"rev15: licensing narrative corrected with measured evidence — ClearlyDefined 97-100% not_found across all ecosystems on a random universe sample; clean-license spine is deps.dev + registries + forge metrics, raising the weight of the ecosyste.ms commercial-license question."`

Handler: `push_site_revision` in
`tools/dashboard/plugins/mission_control/entrypoints/api.py`, backed by
`tools.dashboard.dao.mission_control_db.push_site_revision` — appends an
immutable revision (`revision_seq = MAX+1`) and updates the mission's
current-revision pointer in the same transaction.

## 3. View it

```
https://host.docker.internal:8080/missions/<mission_id>
```

No dashboard furniture, stable across every future push. The response is
marked uncacheable end to end (`Cache-Control: no-store, no-cache,
must-revalidate, max-age=0`) and always reads the current revision fresh from
storage: a push is visible on the next request, with no caching window to wait
out.

**Your document is carried byte for byte, with the platform's navigation
composed around it** (§10). Nothing rebuilds it in an iframe, injects a
Tailwind/Alpine CDN, chops it into slides, or rewrites a single tag of what
you wrote — unlike Present's viewer. What is added is a top bar and the Q&A
surfaces, prepended as their own runtime; your markup is untouched and your
scripts, load events and in-page anchors all behave normally.

The same is true in a Content Frame: one composed document, one function
building it, so the two surfaces cannot disagree.

## 4. Roll back without re-pushing

If a pushed revision is bad, don't re-push the old content — that fabricates
a new revision instead of recording what actually happened. Roll the
current pointer back to any prior revision by id:

```bash
curl -sk https://host.docker.internal:8080/api/missions/<mission_id>/site/revisions/<revision_id>/activate -X POST
# → {"revision": {"revision_id": "<revision_id>", "revision_seq": N, ...}}
```

## 5. Other routes

```bash
GET    /api/missions                                              # list all missions
GET    /api/missions/<mission_id>                                 # mission + current revision metadata
DELETE /api/missions/<mission_id>                                 # hard-delete a mission and its full history
GET    /api/missions/<mission_id>/site                            # current revision, metadata AND content together
GET    /api/missions/<mission_id>/site/revisions                  # history (no content — id, seq, note, byte size, created_at)
GET    /api/missions/<mission_id>/site/revisions/<revision_id>    # one historical revision, full content
POST   /api/missions/<mission_id>/status                          # set lifecycle status -- {"status": "active"|"paused"|"complete"}
```

No membership-set indirection, no `graph set remove` needed for deletion —
`DELETE /api/missions/<id>` is a real route.

Status is explicit and coordinator-set, never inferred — a mission that's
genuinely done looks identical to one that's stalled, so guessing from
staleness would be actively misleading. Defaults to `active` on creation.

```bash
curl -sk https://host.docker.internal:8080/api/missions/<mission_id>/status \
  -X POST -H 'Content-Type: application/json' -d '{"status": "paused"}'
```

## 6. Q&A — visitors ask, you answer

A person viewing the mission site can ask a question; you get notified over
CrossTalk; you answer via the API; the question and your final answer become
part of the mission's permanent record. Every question is attributed to the
person who asked it, and every answer to you.

### Mint a Content Link for a person (you do this, once per person)

```bash
curl -sk https://host.docker.internal:8080/api/visitor-tokens \
  -X POST -H 'Content-Type: application/json' -d '{"display_name": "Jamie"}'
# → 201 {"visitor": {"token": "<64-hex-char secret>", "participant_id": "guest:<uuid>", "display_name": "Jamie"}}
```

Hand them `https://.../missions/<mission_id>?as=<token>`. `token` is a
bearer secret — copy it into the link once and don't log it anywhere else;
`participant_id` is safe to see in conversation history and isn't usable to
impersonate them (the cookie authenticates by token, never by
participant_id — see the schema note in
`tools/dashboard/dao/mission_control_db.py::resolve_visitor` if you're
touching this code). Global, not mission-scoped: the same token works
across every mission's site.

First visit resolves the token and sets an HttpOnly cookie so the URL
doesn't need to keep carrying it; the token also still works as a live
`?as=` query param on the ask-question API call itself, if your site's own
JS wants to pass it explicitly rather than rely on the cookie.

### Receiving a question

You'll get a CrossTalk message from `mission:<mission_id>` when someone
asks — delivery is fire-and-forget (their `POST` returns immediately;
CrossTalk send happens after, so a slow/offline coordinator session never
makes a visitor wait). If delivery itself fails (bad session, transport
error), it's recorded on the entry as `relay_status: "failed"` — but the
question is ALWAYS stored regardless of relay outcome, so poll
`GET /api/missions/<id>/questions` periodically as your real backstop, not
just the CrossTalk ping.

```bash
curl -sk https://host.docker.internal:8080/api/missions/<mission_id>/questions
# → {"questions": [{"entry_id", "question", "asked_by_label", "answer": null, "relay_status", "created_at", ...}]}
```

### Answering

```bash
curl -sk https://host.docker.internal:8080/api/missions/<mission_id>/questions/<entry_id>/answer \
  -X POST -H 'Content-Type: application/json' -d '{"answer": "Q3 2026."}'
```

Records the FINAL answer only — not your working/reasoning, matching how
Present's library never showed raw session state either. `answered_by_session`
is snapshotted from the mission's `coordinator_session` at the moment you
answer, not whoever created the mission — if a mission changes hands,
history correctly shows who actually answered each question.

Attribution (`asked_by_label`) is a snapshot at ask time too — if you
reissue someone a new display name later, their past questions still show
what they were called when they asked.

### Reopening — if the asker isn't satisfied

A visitor who doesn't like your answer can push back:

```bash
curl -sk "https://host.docker.internal:8080/api/missions/<mission_id>/questions/<entry_id>/reopen?as=<token>" \
  -X POST -H 'Content-Type: application/json' -d '{"followup": "That does not match what I saw in the logs -- can you check the retry path too?"}'
```

Same `entry_id` — this is not a new question thread stacking up under the
old one. Reopening clears `answer` back to open and folds your previous
answer plus the new follow-up into working context you'll see on the next
CrossTalk relay; write ONE new answer that integrates the whole discussion,
not a reply to just the latest line. Once you re-answer, the intermediate
back-and-forth is gone — `GET .../questions` and the decision log (§8) both
only ever show the current question/answer pair, never the rounds it took
to get there. This can repeat any number of times; the record stays one
row regardless.

## 7. Presence and "what changed"

The dashboard home page shows, per mission, who's currently aware of it and
what happened since the operator last looked. Neither of these is
Mission-Control-specific machinery — they're the plugin's first real
consumer of platform-wide substrate other plugins already use, wired at the
mission level rather than the whole-page level.

**Presence.** One `Presence.alpine()` surface per mission —
`surfaceId: "mission:" + mission_id` — not one shared surface for the whole
plugin page (that's `coordinator_board`'s pattern; `presentations`'
per-resource `presentations:<designId>` is the one Mission Control mirrors).
If you're writing an agent-side presence row against a mission surface
yourself (rather than relying on the dashboard page), see
`graph://dff97eec-c59` for the substrate contract — this doc only covers the
`mission:<id>` convention, not the presence write path itself.

You don't need to do this manually for the common case: `push_site_revision`
and `answer_question` already write a one-shot presence touch for
`coordinator_session` on that mission's surface, so a coordinator who's
actively pushing revisions or answering questions shows up in presence with
zero integration on their side. It's best-effort and silent on failure —
never blocks or fails your request. This only covers those two calls, not
"coordinator is thinking/working" in general.

**"Since last visit."** A single, implicit watermark per mission — not
per-viewer. There is no per-participant identity system yet (dashboard auth
is a single-personal-root gate; real multi-user org-member identity is a
separate, deferred epic), so whoever opens a mission's detail panel first
consumes the delta for every other viewer. Revisit once real multi-viewer
identity exists.

```bash
curl -sk https://host.docker.internal:8080/api/missions/<mission_id>
# → {"mission": {..., "since_last_visit": {
#     "last_seen_at": <unix ts, or null if never marked seen>,
#     "revisions": [...],   # pushed after last_seen_at
#     "questions": [...]}}} # asked after last_seen_at

curl -sk https://host.docker.internal:8080/api/missions/<mission_id>/seen -X POST
# → {"ok": true, "seen_at": <unix ts>}
```

A mission that's never been marked seen returns an empty delta, not its
whole history — a first-ever view showing the entire past as "new" would be
noisy and misleading. `POST .../seen` is a deliberate action, not a side
effect of `GET .../missions/<id>` — the dashboard's own list page already
calls that GET incidentally on every load just to hydrate summary fields; if
the watermark advanced there, the delta would be erased before anyone saw
it.

Handlers: `get_mission`/`mark_mission_seen` in
`tools/dashboard/plugins/mission_control/entrypoints/api.py`, backed by
`mission_last_seen` in `tools.dashboard.dao.mission_control_db`.

## 8. Pillars — sub-missions with their own coordinator

A large mission (data pipeline + schema + delivery + API/UI, say) doesn't
have to live in one 55,000-word binder. Split it into **pillars**: each one
a first-class sub-mission with its own `coordinator_session`, its own
site-revision history, its own presence surface, scoped conversation
anchored to specific artifacts. Every pillar route below is a structural
mirror of the mission-level route it corresponds to — same shape, one
level down.

**Generic infra, not a fixed pillar list.** Nothing here assumes a
particular number or kind of pillar. You decide the split for your own
mission (this doc's examples use a 4-pillar OSS-data-product breakdown, but
that's illustrative, not prescriptive) and create exactly the pillars you
need.

### Create pillars (the top-level session does this, once per pillar)

```bash
curl -sk https://host.docker.internal:8080/api/missions/<mission_id>/pillars \
  -X POST -H 'Content-Type: application/json' \
  -d '{"name": "Dataset & Schema", "coordinator_session": "auto-schema-abc", "color": "#34d399"}'
# → 201 {"pillar": {"pillar_id": "<uuid>", "mission_id": "...", "name": "...", "coordinator_session": "...", "color": "...", "created_at": ..., "current_revision_id": null, "status": "active"}}
```

`color` is a free-text hex/CSS color hint for the mission dashboard's
pillar-grid dot and your own site's chrome, if you want visual consistency
between the two — purely cosmetic, no validation.

### A pillar's own site (identical shape to a mission's)

```bash
curl -sk https://host.docker.internal:8080/api/pillars/<pillar_id>/site \
  -X POST -H 'Content-Type: application/json' \
  -d '{"html": "<full self-contained HTML>", "note": "rev3: resolver design finalized"}'
# → 201 {"revision": {"revision_id", "revision_seq", "note", "created_at", "byte_size", "pillar_id"}}
```

```bash
GET    /api/pillars/<pillar_id>                                    # pillar + current revision + since_last_visit
DELETE /api/pillars/<pillar_id>
POST   /api/pillars/<pillar_id>/status                             # same {"status": "active"|"paused"|"complete"} as missions
POST   /api/pillars/<pillar_id>/seen
GET    /api/pillars/<pillar_id>/site
GET    /api/pillars/<pillar_id>/site/revisions
GET    /api/pillars/<pillar_id>/site/revisions/<revision_id>
POST   /api/pillars/<pillar_id>/site/revisions/<revision_id>/activate
GET    /missions/<mission_id>/pillars/<pillar_id>                  # direct-serve one screen
```

`GET /missions/<mission_id>/pillars/<pillar_id>` is a convenience URL for
bookmarking/refreshing on one pillar once you're already inside a mission —
**it is never the link you hand out**. See "one link per mission" below.

### One link per mission, not one per pillar

The Content Link you mint (§6) and hand to a person is always the
**mission-level** link. (A Content Link carries a screen to a reader. It is
not a Join Link, which brings someone into an organization -- different
credential, different gate, different protocol.) A guest navigates to a specific pillar from the
platform's own top bar (§10), which lists every pillar with its open-question
count — you do not build that, and there is no separate onboarding step per
pillar.

### Anchored conversation — one call, no `kind` to pick

Same shape as mission-level Q&A (§6), scoped to a pillar, with an optional
`anchor` — free text your own HTML defines to say what specifically is
being discussed (a table name, a screenshot id, an API field):

```bash
curl -sk "https://host.docker.internal:8080/api/pillars/<pillar_id>/questions?as=<token>" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"question": "Why three trigger paths instead of one?", "anchor": "table:oss_purl_resolution"}'
```

```bash
GET    /api/pillars/<pillar_id>/questions
POST   /api/pillars/<pillar_id>/questions/<entry_id>/answer          # {"answer": "..."} -- exactly one, final
POST   /api/pillars/<pillar_id>/questions/<entry_id>/update          # {"text": "..."} -- any number, doesn't close it
POST   /api/pillars/<pillar_id>/questions/<entry_id>/reopen          # {"followup": "..."} -- guest pushback, see §6
```

There's no `kind` field (question/proposal/comment) — a message is a
message, tracked with a reply. Don't invent a taxonomy the API doesn't
have; typing it would just be a place to be wrong for no benefit.

**Reopened questions arrive the same way, with context attached.** If a
guest wasn't satisfied and reopened (§6), your CrossTalk relay looks like a
brand-new question but includes a "prior context" block with the old
answer and the follow-up. File one new answer that replaces the old one —
see §9 for how to actually write it.

**Delivery.** A message on a pillar screen goes to **both** that pillar's
`coordinator_session` (you — a reply is expected) **and** the mission's
top-level `coordinator_session` (copied, tracking only, no reply expected
from them). A mission-level message (no pillar) goes to the mission's
coordinator only. Your CrossTalk envelope tells you which role you're in.

**Only answer once it's actually correct — not provisionally.** An entry
with no `answer` yet is simply open; there's no separate "processing"
status to set. If a task will take a while (redesigning a screen,
re-running an experiment), post interim visibility as many times as you
want without closing the question out:

```bash
curl -sk https://host.docker.internal:8080/api/pillars/<pillar_id>/questions/<entry_id>/update \
  -X POST -H 'Content-Type: application/json' -d '{"text": "capturing the new screenshot now"}'
```

Then file exactly one concise closing answer when it's genuinely done.

### The cross-pillar decision log — Mission Control's job, not yours

The top-level mission tracks every unit of work as it lands across every
pillar — you don't maintain this yourself:

```bash
curl -sk https://host.docker.internal:8080/api/missions/<mission_id>/decision-log
# → {"decision_log": [{"log_id", "mission_id", "pillar_id", "pillar_name",
#     "kind": "revision"|"answer", "revision_seq", "text", "created_at"}, ...]}
```

This is a computed rollup (every revision pushed anywhere in the mission,
every answered question), newest first — not an editorial "these were the
real decisions" judgment. If you want an explicit decision distinct from
routine progress, say so in your `note`/`answer` text; the log surfaces
what you wrote, it doesn't interpret it.

### Idle nag — you'll get reminded if you forget to answer

If a coordinator (mission- or pillar-level) has an open question and goes
idle for about a minute, Mission Control sends a CrossTalk nag listing what's
outstanding. This is separate from your own `graph set-nag` configuration
(if you have one) — it won't touch or override it. You don't need to do
anything to opt in or out beyond actually answering your open questions.

### Presence — same convention, one surface per pillar

`surfaceId: "pillar:" + pillar_id`, exactly like a mission's
`"mission:" + mission_id` (§7). `push_pillar_site_revision` and
`answer_pillar_question` already write a presence touch for your
`coordinator_session` automatically, same zero-integration deal as
mission-level.

## 9. Writing the record — no story-telling

This applies to every field meant to persist: an `answer` (§6/§8), a site
revision's `note` (§2), anything that ends up in the decision log (§8).
The platform enforces the mechanics (one current answer, updates dropped
once you file it, a computed decision log); it can't enforce good writing.
That's on you, and it matters because the record is what everyone else —
the operator, other pillars, a guest who scrolls back — actually reads.
Write it like the operator will only ever see this one line, never the
session transcript behind it.

**Progress updates** (`POST .../update`) are scratch, not a diary. One
short, present-tense line — "checking the acquisition log", "capturing the
new screenshot" — only when a task is genuinely going to take a while and
you want to give visibility while it does. They vanish the moment you
answer (§6/§8's "Only answer once it's actually correct"), so never put
information in one that the answer itself needs; if it matters, it belongs
in the answer.

**Answers** are a conclusion, not a transcript. State the current fact or
decision and its rationale as it stands right now. Don't narrate the steps
you took ("first I checked X, then I tried Y, then..."), don't reference
your own earlier attempts or the back-and-forth that led here ("as I
mentioned", "following up on my last message", "to summarize the
discussion above"). If the question was reopened (§6) and this is your
second, third, Nth answer on the same entry, write it exactly as if it
were the first and only answer — it will be read as exactly that, since
nothing else survives to give it away.

**Revision notes** (§2) get the same treatment: one line, what the current
push actually is, not a log of everything you tried before landing on it.
"Switch to acquisition-run partitioning" — not "iterated a few times,
tried per-ecosystem first, settled on acquisition-run after discussing with
Jeremy."

### Your pillar's status line — the last productive thing done

One field, on your pillar, replaced whenever something finishes. It is the
one line an operator reads to decide where to spend their attention, and it
is rendered in full — never truncated — so its length is your discipline,
not the UI's.

**Write:** the last thing that was actually finished, in words someone who
has never opened this repo would understand.

**Two sentences, maximum.** The first says what got done. The second, if you
use it, says what that means or what it unblocked — in the same plain words.

Every one of these is a hard rule. A summary that breaks any of them is
wrong and should be rewritten, not shipped:

1. **Past tense, and finished.** Something completed, not something underway.
   If nothing finished today, the last thing that finished still stands —
   leave it. A stale true line beats a fresh empty one.
2. **No identifiers, ever.** No file paths, function or class names, table or
   column names, bead IDs, session names, branches, PR numbers, routes, env
   vars, or flags. If it is a token you could grep for, it does not go here.
3. **No jargon.** The test is your reader, not the word. A term someone
   working in this field already uses is not jargon — write it. A term that
   only means what it means inside this project — a component name, an
   internal abbreviation, something you coined — does not go in, however
   natural it has become to you.
4. **No people, and no blockers.** Not who you are waiting on, not who owes
   what, not "blocked on Jeremy". If you need a human, that is what an open
   question is for — asking one is the action, saying you are stuck is not.
5. **No status words as content.** "In progress", "ongoing", "continuing",
   "working on", "on track", "blocked" carry no information. The row already
   shows how long it has been and how many questions are open.
6. **No numbers that need context.** "3 of 8 endpoints", "97% coverage" mean
   nothing to a reader who does not know the denominator. "About a hundred
   times faster" is fine, because it stands on its own.

**The test, before you push it:** could someone who has never seen this
mission read your two sentences and learn something true about where the
work stands? If it only lands for someone who already knows, rewrite it.

**This replaces; it is not a log.** There is no history and nothing
accumulates. Push a new one when something new finishes.

#### Worked examples

Good:

> Ran the prototype over a full batch and measured how fast it goes and how
> much disk it needs. Found and fixed two bugs in a library we depend on,
> which made it about a hundred times faster.

> Agreed how customers will find out which datasets they can download, and
> checked that against what the servers actually do today.

> Got the customer-facing interface running and logged into it inside a test
> container. Confirmed we can demo it without needing the real backend.

Wrong, and why:

| Written | Why it fails |
|---|---|
| "Blocked on Jeremy for the S3 decision." | Names a person and states a blocker (4). If you need that answer, ask a question. |
| "Refactored `_resolve_mission` to call `compose_screen`; tests green." | Identifiers (2); says nothing to anyone outside the code. |
| "Continuing work on the ingest pipeline." | Nothing finished (1), and "continuing" is a status word (5). |
| "Made good progress on the schema." | True of every day; carries no fact. |
| "auto-i9jx7 done, moving to auto-3gwhe." | Bead IDs (2). Reads as an internal ticket queue. |
| "Landed the DAO layer and wired the FE grid." | Jargon (3). |

**Why the rule is this strict:** computer tokens are infinite, which makes
their value zero; human attention is the constrained resource this whole
system exists to protect. Everything else on that row — the age, the open
count, who is present — the platform computes for free. This one line is the
only thing on the screen that costs a coordinator anything to produce, and
it is the only thing on it a human cannot get any other way.

#### Writing one

```bash
curl -sk https://host.docker.internal:8080/api/pillars/<pillar_id>/last-done \
  -X POST -H 'Content-Type: application/json' \
  -d '{"last_done": "Ran the prototype over a full batch and measured how fast it goes and how much disk it needs. Found and fixed two bugs in a library we depend on, which made it about a hundred times faster."}'
# → 200 {"pillar": {..., "last_done": "...", "last_done_at": <unix ts>}}
```

Push it in the same breath as the site revision that made it true — the
screen and this line describing it should never disagree. An empty string
clears it. 400 characters is the cap, which a real one never approaches.

**Why this matters more here than it might elsewhere:** the decision log
(§8) is computed straight from these fields — there is no separate
editorial pass that cleans them up before anyone reads them. Whatever you
write is, verbatim, what lands in the permanent cross-pillar record. Write
the version you'd want to read cold, six months from now, with no memory
of the conversation that produced it.


## 10. What you push, and what the platform adds

Your whole contract is two lines:

1. **Push a complete, self-contained HTML document** for the screen (§2 for a
   mission, §8 for a pillar).
2. **Optionally** mark discussable elements `data-mc-anchor="<short-id>"`, and
   add `data-mc-ask="<question>"` to any of them where you need an answer.

That is all of it. Ordinary HTML, CSS, `<script>`, in-page `#anchor` links —
all work natively, on the dashboard and in a Content Frame alike. Your
document is served **byte for byte**: nothing parses it, rewrites it, or
reserialises it.

### What the platform puts on your page

You do not build any of this, and you should not duplicate it:

- A **top bar** carrying the screen name, navigation to every other pillar,
  how long since the last push, the open-question count, and who is here. It
  is sticky and owns the top `3rem` of the page.
- **Panels** listing the pillars and every open question across the mission,
  and a **full-screen discussion view** for one question — with ask, answer,
  and reopen.
- An **icon control inside every `[data-mc-anchor]` element**, showing the
  number of questions on that artifact and colouring it while any is open —
  or, where you added `data-mc-ask`, an **Answer** control instead.

Two consequences worth stating plainly:

- **Do not build your own pillar navigation, question list or Q&A widget.**
  The platform renders all three, on every screen, for free. A hand-rolled
  one competes with the real one for the same job and the same screen space.
  This is about duplicating the chrome, not about linking: a link to another
  screen from inside your prose, where it belongs to the sentence around it,
  is content. Write those freely.
- **Do not position anything fixed at the very top of the page.** That strip
  belongs to the bar.

### The anchor

`data-mc-anchor` is the one hook you add. Put it on an element that can hold
an extra inline child — a table cell, a figure, a paragraph, a card:

```html
<div class="card" data-mc-anchor="table:oss_purl_resolution">
  <h3>oss_purl_resolution</h3>
  <p>One row per package, keyed by canonical purl.</p>
</div>
```

The control mounts **inside** that element, never as a sibling — a sibling
would break your own adjacent-sibling (`+`) CSS rules. The value is free text
that means something to you; it is what the question gets tagged with.

### Asking the reader something

An anchor on its own is a place to be asked about. Add `data-mc-ask` and the
same element becomes a question **you are asking the reader**:

```html
<div class="card" data-mc-anchor="decision:partitioning"
     data-mc-ask="Partition by acquisition run, or by ecosystem?">
  <h3>How the dataset is partitioned</h3>
  <p>Either works for the loader; they differ under re-ingest.</p>
</div>
```

Use it for a decision you cannot make yourself (§11's "a standing decision you
now think is wrong", and anything across a boundary you do not own). Do not use
it for rhetorical questions in your prose — every one of these is a real
request for someone's attention.

#### The form of an ask

The text of `data-mc-ask` is not a label for a topic. It is the whole of what
the reader is shown: it heads the screen where they type their answer, and it
is the first line of the message that reaches you. Nothing else travels with
it. Whatever it does not say, they do not have.

Every one of these is a hard rule. An ask that breaks any of them is wrong and
should be rewritten before the screen is pushed:

1. **It is a question, and it ends in a question mark.** "Member-rekey policy"
   is a subject heading. "Should a member's keys be re-minted on rekey, or
   carried over?" is an ask.
2. **It names the options.** A fork with its branches stated can be answered in
   one word; a fork with only its name can only be answered with "what are you
   asking me?".
3. **No identifiers.** No bead ids, no file or function names, no internal
   codenames, no route names. If it is a token you could grep for, it does not
   belong in the question. The anchor is where those live, and the anchor is
   never shown to a person.
4. **It is answerable without opening anything else.** The reader is on a
   phone, looking at a list of things waiting on them. If answering requires
   finding a ticket first, it will not be answered.
5. **Say what turns on it, in one clause.** What the choice costs either way,
   or what it unblocks. A decision with no stated consequence reads as trivia
   and gets deferred.
6. **One decision per ask.** Two questions in one box get one answer, and you
   will not know which.

**The test, before you push it:** could someone who has not read your screen
answer it from the sentence alone? If they would have to ask you what you meant,
it is not an ask yet.

**Why this is strict:** these do not queue with the mission's other questions.
They are pulled out into their own count, in front of the one person whose
attention the mission cannot replace, precisely so a real decision is not lost
among conversations. That separation is worth exactly as much as the weakest
item in the pile. Seven half-formed asks are worse than none, because they
teach the reader that the pile is not worth opening.

Good:

> Should witness keys be re-minted on every rekey, or carried over? Carrying
> them over is simpler now and costs a migration if we ever rotate the root.

> Do we buy a real iOS device for testing, or keep relying on the simulator?
> The simulator has not reproduced the two most recent Safari-only faults.

Wrong, and why:

| Written | Why it fails |
|---|---|
| "Member-rekey policy (x97iz)" | Not a question (1), no options (2), carries a bead id (3). |
| "B7: the update mechanism (unblocks 6aamc)" | A codename and an id (3); nothing states what is being decided (1, 2). |
| "Demo resources" | A subject, not a question. Unanswerable without asking you what you mean (4). |
| "Confirm cleanup: close auto-509" | An instruction to yourself with an id in it (3); if you can close it, this is not an ask at all (§11). |
| "Should we defer witness-key persistence, and also decide the rekey policy?" | Two decisions, one box (6). |

Three things follow, and you do not build any of them:

- The control reads **Answer** rather than a question count, and stops the
  moment it is answered.
- It is **not** in the mission's open-question list until answered. That list
  is what the mission owes the reader; this is the other direction. It is
  counted separately, as *"N for you"*, so it is still findable.
- Answered, it becomes an ordinary entry with the attributions the other way
  round — asked by your screen, answered by them — in the same record, with
  the same reopen path. You are told the answer over CrossTalk. You are
  deliberately **not** told the question: you wrote it.

A question exists only on the page until it is answered, so **editing it away
is how you withdraw it.** Nothing to retire, nothing to clean up. Change the
text and you have asked a different question; the answered ones are untouched,
because those are records.

### Name your anchors in words

Whatever heading your anchored element sits under travels with every question
asked there, and is what the reader, your CrossTalk relay and your reminders
all say the question is about. The anchor value itself is never shown to a
person.

So `data-mc-anchor="decision:partitioning"` under a heading of "How the dataset
is partitioned" reads well everywhere. The same anchor under no heading at all
falls back to the raw slug, and a short reply — "Do it" — then reaches you as
two words and an identifier.

**Restructure freely; move the conversations with you.** Anchors are
placement hints, not a contract with the past. A question asked last week is
never a reason to keep content nobody needs — if the subject moved, point the
question at where it lives now; if the subject is gone, retire the question.

What is not acceptable is leaving a discussion pointing at an anchor that no
longer exists. It still lists, but nobody finds it beside the thing it is
about, and nothing tells you it happened.

Check which of your anchors carry questions before you rewrite:

```bash
curl -sk https://host.docker.internal:8080/api/pillars/<pillar_id>/questions
```

Then, for each one, either keep the anchor value on whichever element now
holds that subject, or move it:

```bash
curl -sk https://host.docker.internal:8080/api/questions/<entry_id>/anchor \
  -X POST -H 'Content-Type: application/json' -d '{"anchor": "table:throughput"}'
# {"anchor": null} detaches it: it stays in the record and in the questions
# list, it just no longer belongs beside any particular element.
```

Or retire it, when the subject itself stopped being relevant:

```bash
curl -sk https://host.docker.internal:8080/api/questions/<entry_id>/retire \
  -X POST -H 'Content-Type: application/json' \
  -d '{"note": "the batch view this asked about was replaced"}'
```

Retiring is not deleting. The entry leaves the screen and stops counting as
open; what was asked, what it was answered with, and why it stopped mattering
all stay in the record. Posting an empty note brings it back.

### Sections

Mark each top-level section of your screen and the platform pins a navigator
under the bar: a horizontal strip of section names that scrolls sideways
within itself, marks the section you are currently reading, and keeps that
one in view as you scroll.

```html
<section data-mc-section="Objective and scope"> ... </section>
<section data-mc-section="The work"> ... </section>
<section data-mc-section="Decisions"> ... </section>
```

One attribute. You decide what a section is, what it is called and what goes
in it; the platform decides how that behaves on a phone. Declare none and
there is no strip.

**Do not build your own.** Not because yours would be worse, but because two
pinned bars that do not know about each other collide: yours pins at the top
of the viewport, the platform's bar is already there, and yours disappears
underneath it. The platform can stack them only when it owns both.

### The two things that do not work, and why

**Scripts must be inline.** A Content Frame is served under a CSP that permits
inline script, not arbitrary `https:` sources — and an external
classic script would not preserve execution order relative to your inline
ones anyway. Paste the code in; do not reference `/static/...`.

**Screen data arrives from the platform, not from `fetch("/api/...")`.** Read
through a Content Link, your document runs in a **Content Frame**: sandboxed,
with no origin. There is no dashboard to call -- a relative URL has nothing to
resolve against and no credential to carry. The platform delivers the pillar list, the conversation
and presence with the document, and renders them itself. Your page's job is
the content; the state around it is not yours to fetch.

Everything else — your layout, your styling, your interactivity, your data
baked into the page at push time — is entirely yours.

## 11. How to run a pillar

Your job is to drive your pillar's work as deep as it will go, and come back
knowing what you did not know when you started.

**Build to find out.** You do not discover what is genuinely unresolved by
planning; you discover it by trying to build the thing and hitting the point
where you cannot proceed without deciding something nobody has decided. That
point is the valuable output. Planning finds the questions you already knew to
ask.

**At a fork you cannot resolve, choose and continue.** Pick the option you
would defend, write down that you picked it, and keep going. Do not stop and
wait for an answer — a question you could have answered provisionally, turned
into a block, spends the one resource the mission cannot replace.

**Stub what you cannot build yet.** A stand-in that lets the next stage run is
worth more than a real implementation that never gets exercised, because the
stand-in tells you whether the stage after it works.

**Some experiments are worth running and not worth keeping.** When the fastest
way to answer a question is to make the real change and see what happens,
make it, learn the answer, and take it back out. The finding is the output;
the artifact was only ever the instrument. Say that you did it and what it
proved — "tried it, it works, reverted because the shape is not mine to
decide" is a stronger claim than any amount of arguing that it would probably
work.

**Three things look like forks and are not all the same.** Choose-and-continue
applies to the first only:

- **A fresh fork** — nobody has decided this. Pick the option you would
  defend, record it, keep going.
- **A standing decision you now think is wrong.** Do not quietly choose
  against it: propose the correction, say plainly what evidence changed your
  mind, and leave the decision where it lives. Reversing someone else's
  settled call silently is how two pillars end up building incompatible
  things while both believe they are compliant.
- **Something you cannot do at all** — no write access to the repository the
  code belongs in, a tool the work requires that is absent, a dependency that
  does not exist yet. This is not a choice and no stand-in substitutes for it.
  Record it as a blocker with the evidence, and say what it stops. Trying
  harder is not the answer and neither is picking an option.

**If others will build against something you own, publish its literal shape
early.** A decision about how an interface behaves is not enough for anyone
writing against it: they need the actual field names, the actual path, the
actual envelope. Publish a concrete stand-in, marked plainly as provisional,
as soon as the behaviour is agreed. Two pillars guessing independently produce
two incompatible guesses, and both find out late.

The same point from the deciding side: **say what level a decision is settled
at.** A decision marked settled reads as buildable, and one settled on
behaviour alone is not — whoever builds against it has to invent the interface
and will not know they were inventing. If the behaviour is agreed and the
shape is not, say exactly that, so the gap is visible instead of being
discovered by two pillars separately filling it in.

**Drive to end-to-end.** The target is the whole flow exercised, start to
finish, with every unresolved thing standing in as a stub. Every stage having
run once — even against stand-ins — is what proves the shape is right.

**A gate on shipping is not a gate on prototyping.** A mission that holds code
until contracts land is holding what gets committed, merged and depended on —
it is not telling you to stop finding things out. Build the throwaway outside
the repositories it would eventually live in, commit nothing, and say plainly
that is what you did. Running the thing once is how a contract gets written
from evidence instead of from argument.

**Then harvest.** Walk back over every stub and every choice you made without
a specification. Each one is either a decision you took (record what you chose
and why) or a question you cannot settle (record what it blocks). That set is
most of what your screen says.

**When to stop.** Stop when driving further would only refine something
already understood; when a fork genuinely cannot be guessed and everything
behind it depends on the answer; or when the next thing to resolve sits across
a boundary you do not own. Say which you hit. The third is not a failure to go
deeper — it is the work correctly reaching its edge, and it belongs to the
coordinator to route rather than to you to guess. Do not keep
polishing a stage that already works — depth into the unknown is the point,
not finish on the known.

A screen with little to say usually means the work has not been driven far
enough to find out what is unknown, not that it was written up badly.

**Not every pillar builds.** A pillar whose deliverable is a record, a
decision, or a body of research has no loop to run: there is nothing to stub
and nothing to drive end-to-end. Neither does the coordinator, which does none
of the mission's work by design (§13). If that is you, the depth that matters
is in the material itself — is the record complete, is it accurate, does it
still say something a reader needs. Say plainly that the loop does not apply
rather than performing it. A thin screen backed by that statement is a
correct outcome, not an under-driven one.

## 12. What your screen says

Five things, in this order — a shape, not a form to fill in. A section with
nothing real in it fails the relevance test below as surely as any other empty
content, so leave it out rather than writing a heading over a blank. Most
pillars have all five; some honestly have two.

**A. Objective and scope.** What this pillar is for, what done looks like, and
what it explicitly does not own.

**B. The work.** Every unit of development your pillar needs, each with a
headline and a summary that stand alone, and detail available underneath.
Someone reading only the headlines should understand the shape of the work.

The writing rules bite hardest at the top. A headline and a summary have to
carry a reader who knows nothing about the internals, so they hold no
identifiers at all. The detail underneath is where someone has chosen to go
deeper: it can name real things, and where an exact string, message or value
IS the evidence, quote it verbatim rather than describing it. Plain language
is still the default there — technical is not permission to be unreadable.

**C. Decisions.** Every decision taken under uncertainty: what the fork was,
which way you went, why, and what breaks if the other way turns out to be
right.

**D. Open questions.** What is still unresolved, what each one blocks, and
what would settle it. A question a human can answer in one pass — options and
consequences stated — gets answered. A vague one waits.

**E. Where it stands.** Current state, what is in flight, what is blocked.

C and D are the mission's most valuable output. They are the difference
between a reader knowing what happened and a reader being able to act.

### What earns a place

Include something only if a reader would act or think differently for knowing
it. Concretely, it must do at least one of: define or change a deliverable;
inform a decision somebody still has to make; establish or change a functional
requirement; surface a real problem with the design; or change the picture of
what is done, what is left, or what is unknown.

If it does none of those, it is internal record-keeping. It belongs in your
own notes, not on the screen.

**Effort is not relevance.** That something was hard, took a long time, or was
finally solved after a struggle argues for its importance to you and says
nothing about its importance to the reader.

**Relevance decays.** Judge every item against the state of the work now, not
against how much it mattered when it was written. While you are setting up
your environment, that is genuinely your current state and belongs on the
screen; once it is working, it collapses to a line listing what you depend on
and the fact that it is met. Demoting is not deleting — it moves to your own
notes.

Keep the screen at the size a human will actually read. That is the target,
not the smallest possible screen.

## 13. The mission controller

One session coordinates the mission and does none of its work. That is not a
limitation — every pillar is immersed in its own task, and immersion is
exactly what makes it a poor judge of which of its own details matter to
anybody else. The controller is deliberately the one participant who is not
immersed.

It keeps up with what every pillar is actually doing, holds the only complete
picture of the mission, owns the mission overview screen, orchestrates the
interfaces between pillars, and decides anything that crosses a boundary —
shared schemas, naming, interface shapes. On those, **you propose; the
controller decides.** That is what lets every pillar move at full speed
without the mission contradicting itself.

It reviews every revision you push, asking three things you are not positioned
to ask about your own work: is this redundant with what another pillar already
says, is it irrelevant to the mission, and is it phrased so that only its
author can read it?

Expect edits. A review that changes nothing has not happened.

A review outcome goes two places: to you, so you can act on it, and into the
mission's own signpost note, so the mission keeps a record of what was cut and
why. A judgement that lives only in a conversation is lost to the next pillar
that would have made the same mistake.

### The mission check-in

The operator's Activity screen has an Attention tab: a timeline of where their
attention went, which agents write. The mission check-in is the controller's
entry on it. It answers one question — **what happened on this mission?** —
for someone who was not watching.

Write one when a body of work has landed. Not per commit, not per pillar
report; a stretch of the mission with a shape to it.

```bash
graph journal write "Mission name check-in — N commits, largest advance in <area>" \
    --normal /tmp/normal.md --expanded /tmp/expanded.md \
    --start <ISO> --end <ISO> --type mixed
```

Use `--start`/`--end` for the period being reported. A `--since` lookback
timestamps the entry at the start of the window, which files a check-in behind
shorter entries covering less time.

The three bodies are one report at three depths: the headline stands alone in a
list of a day, `normal` is what happened, `expanded` is the full check-in.

**The shape of the expanded body.** Five sections, in this order, and no others:

```
N commits landed. Largest advance in <area> — <one line on why>.

## Live UI Changes
- What the operator can now see and use that they could not before.

## New Platform Functionality
- **Named capability.** What it is, in a sentence that survives without
  the bead that produced it.

## Plans Changed
- **What changed, and what moved because of it.** A directive, a scope cut,
  a decision held. Name the pillars it moved and what each one did.

## Blockers Encountered
- **What is blocked and why.** Evidence, not a feeling. Who it belongs to
  if it is not yours.

## New Beads Written
N today, grouped by the work they belong to — the shape of what was written,
not a list of titles.
```

**Writing it.** Technical manual voice: complete declarative sentences,
mechanisms named precisely, no narrative arc and no closing line drawing a
moral. Everything else follows from one rule — **say what happened, not the
process that produced it.** A directive is worth a line under Plans Changed
because of what it moved; the audit it triggered is not.

Three things that do not go in it, at any depth:

- **No disclaimers.** Not what the tracker failed to record, not how the
  numbers were derived, not what you could not verify. Fix those or raise
  them; a check-in is not where they live.
- **No process.** Reviews, calibration, who asked whom. The reader wants the
  product, not the machine that made it.
- **No hedging on the count.** Report what landed. If a number is wrong,
  find the right one before writing.

### Raising the whole mission, when the whole mission is stuck

Everything above happens inside a mission: the operator opens it and works
through what is waiting there. That is the right shape for the detail — a
pillar's `data-mc-ask` is a specific decision, in context, on the screen where
the work is.

It is the wrong shape for "this mission cannot proceed". Waiting to be visited
is not how you say that.

**The controller — and only the controller — writes one row saying where the
mission stands.** It lands in the operator's Activity → Notifications, where
they see it without opening any mission. It is the mission's push channel.

```bash
graph set add dashboard.activity.ask#2 --key "$AUTONOMY_SESSION" --from - <<'JSON'
{
  "session_id": "auto-0729-212913",
  "compact": "Multi-User Autonomy is blocked on one decision: relay-hosted join pages.",
  "normal": "Three pillars are waiting on whether the relay may host the join page...",
  "expanded": "...the longer version, for when they want the whole picture.",
  "created_at": "2026-08-14T01:00:00Z",
  "revision_seq": 4
}
JSON
```

One row per session by construction — writing again replaces it. So this is
not a feed and nothing accumulates: it is the single current answer to "what
does this mission need from you?", rewritten whenever that changes, and
removed when the answer is nothing.

The three bodies are one thought at three depths, and `compact` has to stand
alone — it is what a reader sees in a list of every session wanting something.
Bump `revision_seq` on every rewrite; a refresh request from the operator
clears only when it advances, which is how they ask you to re-prove an ask is
still live.

**A pillar never writes one of these.** Your mission-level status belongs to
the controller, who is the only participant holding the whole picture (that is
this section's whole argument). Raise it to them and let them decide whether
the mission is blocked or only you are.

---

## How this document is maintained

This skill is read by coordinators who have just been created. It is written
for exactly that reader, always. Three things it defines, and nothing else:

1. **The infrastructure** — what Mission Control is and what it does for you.
2. **The workflow** — who does what, when.
3. **The ideal form of the artifacts** — what a good screen, a good answer and
   a good status line actually look like.

**It never tells stories.** These are not stylistic preferences; a revision
that breaks one is wrong and should be rewritten before it lands:

- **No history.** Not what a feature replaced, not what an earlier version of
  this document said, not which release something arrived in. A reader who
  has never seen the old thing gains nothing and is handed a second, obsolete
  model of the system to hold in their head.
- **No transitions.** A guide for moving from how things were to how they are
  is correct exactly once and misleading forever after. Transitions are
  delivered directly, by the mission controller to its pillars, for the one
  set of screens that needs them. They do not belong here.
- **No futures.** Nothing about what does not exist yet, what is planned, or
  what will land later. If a capability is absent, either the document is
  silent about it or it states the present limit plainly, in the present
  tense, with no promise attached.
- **No development phases.** Coordinators do not know or care in what order
  this was built.

**Everything is present tense and current.** Write as though the system has
always worked exactly this way and this is the first anyone is hearing of it.

**Refine it from evidence.** As practice teaches better ways to run a mission,
present a screen or write a record, this document absorbs them — and drops
whatever they replaced, leaving no trace of the earlier advice. Growing more
accurate is the point; growing longer is not. The best version of this
document is the shortest one that still specifies, precisely, the best way we
currently know to run a mission.
