# Mission Control

Mission Control hosts one revisioned site per mission. A mission can contain
pillar sites, questions and answers, presence, status lines, and a computed
decision log. Pushing a site revision publishes it immediately; there is no
separate publish step.

Use the dashboard base URL from the session primer:

- Host-network sessions use `https://localhost:8080`.
- Bridge-network sessions use `https://host.docker.internal:8080`.

## Authentication

Every coordinator API call must send:

```bash
-H "Authorization: Bearer $CROSSTALK_TOKEN"
```

The bearer identifies the calling session and its organization. It does not
grant operator authority. Deleting a mission, deleting a pillar, and minting a
visitor token refuse session callers even when this header is present.

Never write the expanded token value into chat, notes, commits, or other
durable text. Double-quoted shell arguments and unquoted heredoc delimiters
expand `$CROSSTALK_TOKEN`; single-quoted arguments and quoted heredoc
delimiters do not. Guest requests use the visitor's `?as=<token>` credential,
not the session bearer.

The examples below use this authenticated request shape. Set `DASHBOARD` to
the base URL for the current session before using it.

```bash
curl -sk "$DASHBOARD/api/missions" \
  -H "Authorization: Bearer $CROSSTALK_TOKEN" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"name":"OSS Insights","coordinator_session":"'$AUTONOMY_SESSION'"}'
```

## The screen contract

Push a complete, self-contained HTML document. Mission Control serves that
document byte for byte and composes its own interface around it. Ordinary HTML,
CSS, inline JavaScript, load events, and in-page links work normally.

You may add three attributes:

```html
<section data-mc-section="Decisions">
  <div data-mc-anchor="decision:partitioning"
       data-mc-ask="Should we partition by acquisition run or by ecosystem? They differ under re-ingest.">
    <h3>How the dataset is partitioned</h3>
  </div>
</section>
```

- `data-mc-section="Readable name"` adds the section to the platform's pinned
  section navigator. Declare no sections when no navigator is useful.
- `data-mc-anchor="stable-id"` gives questions a placement beside an artifact.
  Put internal identifiers in this value because the reader never sees it.
- `data-mc-ask="complete question"` asks the reader for a decision at that
  anchor.

### Constraints that are not optional

1. The platform owns the top `3rem` of the viewport. Do not place fixed
   content there.
2. The platform supplies the top bar, screen name, pillar navigation, time
   since the last push, open-question counts, presence, question panels,
   full-screen discussion view, and question controls. Do not build another
   copy of them. Anchor controls show their question count and whether any
   question is open; `data-mc-ask` displays an Answer control instead. Links
   to other screens inside your prose are content and remain allowed.
3. The question control mounts inside each element marked with
   `data-mc-anchor`, not beside it. Mark an element that can contain an inline
   child and account for that child in its CSS.
4. Put each anchor below a plain-language heading. Without one, conversation
   context falls back to the raw anchor identifier.
5. Keep scripts inline. A visitor Content Frame permits inline scripts but not
   arbitrary external script sources.
6. Do not fetch relative dashboard API URLs from the screen. A visitor Content
   Frame has no origin or dashboard credential. Bake screen data into the
   document; the platform supplies pillars, questions, and presence itself.
7. Preserve anchor values when content moves. Before removing or renaming an
   anchor, retrieve its questions, then keep the anchor on the new element,
   move those questions, detach them, or retire them.
8. An unanswered `data-mc-ask` exists only in the current HTML. Remove the
   attribute to withdraw the ask. The question-retirement API applies to
   stored conversation entries, not unanswered asks. Changing the ask text
   asks a different question; previously answered records remain unchanged.

### Write an answerable ask

The `data-mc-ask` value is the entire prompt the reader sees. It must:

1. Be one question ending in a question mark.
2. Name the available options.
3. Avoid internal identifiers, file names, route names, and project codenames.
4. Be answerable without opening another artifact.
5. State in one clause what the decision changes, costs, or unblocks.
6. Ask for one decision only.

Use asks only for decisions you cannot make, including a settled decision you
now believe should change and a decision crossing an ownership boundary. Do
not use them for rhetorical questions or tasks you can complete yourself.

Two acceptable asks are:

> Should witness keys be re-minted on every rekey, or carried over? Carrying
> them over is simpler now and costs a migration if the root ever rotates.

> Should we buy a physical iOS device or continue using the simulator? The
> simulator did not reproduce the two latest Safari-only faults.

These asks must be rewritten:

- `Member-rekey policy (x97iz)` is not a question, names no options, and
  contains an identifier.
- `Demo resources` cannot be answered without more context.
- `Should we defer witness-key persistence and also decide the rekey policy?`
  asks for two decisions.

Until answered, an ask appears separately as `N for you` rather than as an
open question owed by the mission. After the reader answers, it becomes a
normal attributed conversation record, can be reopened, and no longer shows
an Answer control. CrossTalk reports the answer but omits the question because
the screen authored it.

## Structured style

A mission chooses one of two rendering styles. `freeform` is everything the
sections above and below describe: coordinators push whole HTML documents.
`structured` replaces only the page's content layer: items live as
`dashboard.mission.item` Settings rows in the mission's organization, and
the platform renders every screen from them with one standard viewer — a
single app covering the overview and all pillars, with cross-pillar
Decisions, Questions and Feed views plus an activity grid computed from
item timestamps. Chrome, questions, presence, and the revision store are
identical in both styles, and the choice is reversible at any time:

```bash
graph mission style <mission> structured    # or freeform
```

In structured mode, drive everything through `graph mission` — never curl,
never a site push:

```bash
graph mission list                          # missions with style + status
graph mission status <mission|pillar> [-v]  # item counts by state, open asks
graph mission items <surface> [--kind decision] [--state open] [--json]
graph mission add <surface> <item-id> --kind work --title "…" \
    [--state proven] [--body -] [--evidence "…"] [--ref commit:abc1234]
graph mission update <surface> <item-id> [--title …] [--body -] [...]
graph mission state <surface> <item-id> proven [--note "what was seen"]
graph mission retire <surface> <item-id>
```

`<surface>` is a mission or pillar — id, id prefix, or name substring.
`--body -` and `--note -` read stdin for multiline prose. Item kinds:
`scope` (one per surface: purpose + "Does not own:"), `work`, `checkpoint`
(ordered arc steps, use `--order`), `decision` (`--fork/--chosen/--if-wrong`),
`question` (`--ask` for an operator-answerable ask), `status` (one per
surface: where it stands), `metric` (`--value "15.2 MiB/s"`), `incident`
(corrections and negative results), `exhibit` (trusted HTML, sparingly).
States: `proven done settled active code_only next specified open blocked
deferred retired` — `proven` means exercised on the real system and
witnessed; `code_only` means tests pass, never run for real.

Attach every item to a pillar, never to the mission surface. The
overview is a computed summary view, not a content surface — mission-level
items render folded as legacy and their grid row disappears once they are
gone. Cross-pillar decisions belong to the pillar that owns the boundary,
with `refs` pointing at the others.

Two platform surfaces still read fields you must feed: keep posting
`last_done` (`POST /api/pillars/<id>/last-done`) when work completes — the
pillar chooser and status feed render it and nothing substitutes for it —
and let `graph mission state` stamp `happened_at` (or pass `--happened-at`)
so the activity grid, feed, and pillar ages reflect when things really
landed.

Rules that carry over from the screen contract: an item's `item-id` is its
anchor — questions bind to it, so never rename one whose subject survives;
ask text must be one answerable question naming its options and consequence;
`graph mission state` stamps the moment the state was earned, which is what
the feed and activity grid render — transition items when things actually
happen, not in batches.

The raw HTTP surface behind the CLI (same bearer rules as everything else):

```text
POST /api/missions/<id>/style                 {"style":"structured|freeform"}
GET  /api/missions/<id>/items                 whole mission, pillars included
GET  /api/pillars/<id>/items
PUT  /api/missions/<id>/items/<item_id>       full item payload
PUT  /api/pillars/<id>/items/<item_id>
POST /api/missions/<id>/items/<item_id>/state {"state":"…","note":"…"}
POST /api/pillars/<id>/items/<item_id>/state
```

## Mission workflow

### Create a mission

`POST /api/missions` accepts
`{"name":"...","coordinator_session":"...","org":"..."}`. Set
`coordinator_session` to the session that should receive mission questions.
It is routing data, not a permission, and must be changed when coordination
moves to another session.

### Push and view a site

Use either route with `{"html":"<complete document>","note":"one-line change"}`:

```text
POST /api/missions/<mission_id>/site
POST /api/pillars/<pillar_id>/site
```

Each call appends an immutable revision and makes it current atomically. The
optional `note` appears in revision history and the decision log. Write one
line stating what changed; put supporting detail in the HTML. The new revision
is visible on the next request.

View the current screens at:

```text
/missions/<mission_id>
/missions/<mission_id>/pillars/<pillar_id>
```

The mission URL is the only link to give a visitor. The platform bar lets the
visitor navigate to every pillar and shows each pillar's open-question count.
The pillar URL is only a direct bookmark after entering the mission.

### Roll back

Activate the earlier revision instead of pushing its old HTML again:

```text
POST /api/missions/<mission_id>/site/revisions/<revision_id>/activate
POST /api/pillars/<pillar_id>/site/revisions/<revision_id>/activate
```

Activation preserves the real revision history. Re-pushing old content would
create a false new revision.

### Set lifecycle and coordination

Mission and pillar status is explicit and accepts `active`, `paused`, or
`complete`; both start `active`, and status is never inferred from activity.
Changing a coordinator changes future question delivery without changing past
records.

```text
POST /api/missions/<id>/status                {"status":"active|paused|complete"}
POST /api/missions/<id>/coordinator           {"coordinator_session":"..."}
POST /api/missions/<id>/org                   {"org":"..."}
POST /api/pillars/<id>/status                 {"status":"active|paused|complete"}
POST /api/pillars/<id>/coordinator            {"coordinator_session":"..."}
```

Everything below a mission inherits its organization.

## Pillars

A pillar is a first-class sub-mission with its own coordinator, site history,
status, presence surface, and anchored conversation. Missions can have any
number or kind of pillars.

Create and list pillars with:

```text
POST /api/missions/<mission_id>/pillars
     {"name":"Dataset & Schema","coordinator_session":"auto-...","color":"#34d399"}
GET  /api/missions/<mission_id>/pillars
```

`color` is an unvalidated CSS color hint. It has no behavioral effect.

## Conversation

Mission-level messages go to the mission coordinator. Pillar messages go to
the pillar coordinator for action and to the mission controller for awareness.
The CrossTalk envelope states which role received the message.

A message has no `kind` field. Do not invent separate question, proposal, and
comment types.

Question storage completes before CrossTalk delivery. Delivery is
fire-and-forget and can fail without losing the question. A failed relay sets
`relay_status` to `failed`; periodically poll the relevant questions endpoint
instead of relying only on CrossTalk:

```text
GET /api/missions/<mission_id>/questions
GET /api/pillars/<pillar_id>/questions
```

### Ask and answer

Visitors create questions with `{"question":"...","anchor":"optional"}` and
their visitor credential:

```text
POST /api/missions/<mission_id>/questions?as=<visitor_token>
POST /api/pillars/<pillar_id>/questions?as=<visitor_token>
```

Coordinators file one final answer with `{"answer":"..."}`:

```text
POST /api/missions/<mission_id>/questions/<entry_id>/answer
POST /api/pillars/<pillar_id>/questions/<entry_id>/answer
```

An answer closes the entry. `asked_by_label` is copied when the visitor asks,
and `answered_by_session` is copied from the current coordinator when the
answer is filed. Later identity or coordinator changes do not rewrite either
attribution.

For long work, a pillar coordinator may post transient visibility with:

```text
POST /api/pillars/<pillar_id>/questions/<entry_id>/update {"text":"checking the acquisition log"}
POST /api/missions/<mission_id>/questions/<entry_id>/update {"text":"checking the acquisition log"}
```

Updates do not close the question and disappear when the final answer is
filed. Never put a conclusion only in an update.

### Reopen

A visitor can reopen an answered entry with `{"followup":"..."}`:

```text
POST /api/missions/<mission_id>/questions/<entry_id>/reopen?as=<visitor_token>
POST /api/pillars/<pillar_id>/questions/<entry_id>/reopen?as=<visitor_token>
```

Reopening keeps the same entry, clears its answer, and sends the prior answer
and follow-up as working context. File one replacement answer that resolves
the whole question. Intermediate rounds disappear after that answer; the
record always contains one current question-and-answer pair.

An asker may add `{"followup":"..."}` to a still-open question through its
`/followup` route. This amends the same question and relays it again. A
coordinator may close a question that no longer needs an answer through its
`/close` route; closing does not invent an answer. A question's wording can be
changed in place through its `/rephrase` route without changing its entry,
answer, or anchor.

### Move or retire a stored question

```text
POST /api/questions/<entry_id>/anchor {"anchor":"new-id"}
POST /api/questions/<entry_id>/anchor {"anchor":null}
POST /api/questions/<entry_id>/retire {"note":"why it stopped mattering"}
```

Detaching leaves the question in the record without placing it beside an
artifact. Retiring removes it from the screen and open count without deleting
its question, answer, or retirement reason. Posting an empty retirement note
restores it.

## Writing the record

Persisted text must make sense without the session transcript.

- Write progress updates as one short, present-tense action. They are
  temporary visibility, not a diary.
- Write answers as the current conclusion and its rationale. Do not narrate
  the investigation or refer to previous attempts. A reopened question's new
  answer must read as the only answer.
- Write revision notes as one line stating what the current push changes. Do
  not duplicate the screen's evidence or narrate how the result was reached.

### Write the pillar's last completed result

`last_done` replaces one status line; it is not a history. Write at most two
sentences and 400 characters:

1. State completed work in the past tense. Leave the prior true line in place
   when nothing newer has finished.
2. Use words a reader outside the repository understands.
3. Do not include file paths, code identifiers, tickets, sessions, branches,
   routes, environment variables, or flags.
4. Do not name people or describe blockers. Ask a question when a decision is
   needed.
5. Do not substitute status words such as “in progress,” “ongoing,” or
   “blocked” for a completed fact.
6. Use numbers only when their meaning is self-contained.

A useful line says what was completed and, optionally, what that result made
possible.

Two acceptable status lines are:

> Ran the prototype over a full batch and measured its speed and disk use.
> Fixed two dependency defects, which made the run about a hundred times
> faster.

> Agreed how customers will discover downloadable datasets and checked the
> decision against the servers' current behavior.

These status lines must be rewritten:

- `Blocked on Sam for the storage decision` names a person and a blocker.
- `Refactored _resolve_mission; tests green` contains an implementation
  identifier and says nothing useful outside the repository.
- `Continuing work on the ingestion pipeline` states no completed result and
  substitutes a status word for one.

An empty string clears the field:

```text
POST /api/pillars/<pillar_id>/last-done {"last_done":"..."}
```

Update `last_done` with the site revision that makes it true so the status and
screen agree.

## Presence and changes since the last visit

Mission presence uses `mission:<mission_id>` and pillar presence uses
`pillar:<pillar_id>`. Pushing a revision and answering a question
automatically touch the current coordinator's presence on the relevant
surface. These touches are best-effort and do not report general thinking or
work between calls.

Each mission and pillar has one shared “seen” watermark, not one per viewer.
Retrieving the resource reports revisions and questions after that watermark;
retrieval does not advance it. Marking the resource seen advances it. A
resource never marked seen reports an empty delta rather than its whole
history as new.

```text
GET  /api/missions/<id>
POST /api/missions/<id>/seen
GET  /api/pillars/<id>
POST /api/pillars/<id>/seen
```

Mission Control sends an idle CrossTalk reminder after about a minute of
inactivity when a mission or pillar coordinator has unanswered questions. This
does not change the session's own nag configuration.

## Visitor links and identity

Minting a visitor token is an operator action. Request approval with kind
`visitor_token`; its request contains `display_name`, an optional
`data:image/...;base64` avatar, and an optional `reason`. The approval result
contains a secret token and a safe `participant_id`:

```text
POST /api/approvals
{"kind":"visitor_token","session":"<current session>",
 "request":{"display_name":"Jamie","avatar":"<optional data URL>","reason":"<optional>"}}
```

Give the visitor:

```text
https://<dashboard>/missions/<mission_id>?as=<visitor_token>
```

The token is global across missions. The first visit sets an HttpOnly cookie,
and the same token can also authenticate an ask through its live `?as=` query
parameter before a cookie exists. Copy the token into the link once and do not
log it elsewhere. Conversation history uses `participant_id`; that identifier
cannot impersonate the visitor.

A Content Link carries a screen to a reader. It is not a Join Link, which adds
someone to an organization.

Forgetting a visitor invalidates the link immediately but preserves the label
on past questions. The visitor lookup returns the name and photo, never the
token. Removing a presence row affects only that surface.

```text
GET    /api/visitor-tokens/<participant_id>
DELETE /api/visitor-tokens/<participant_id>
DELETE /api/presence/<surface_id>/<participant_id>
```

## Decision and status records

The mission decision log is computed from every mission and pillar revision
note and every final answer, newest first. It does not decide which entries are
important. State a decision explicitly in the note or answer when it should
read as one.

```text
GET /api/missions/<mission_id>/decision-log
GET /api/missions/<mission_id>/status-feed
POST /api/missions/<mission_id>/here
POST /api/pillars/<pillar_id>/here
```

The status feed contains every pillar's current `last_done` value. The `here`
routes record presence.

## Run a pillar

1. Build or exercise the work end to end to discover what is unknown. Use
   stand-ins for unavailable stages so later stages run at least once.
2. At a fresh undecided fork, choose the option you can defend, record it, and
   continue.
3. When evidence contradicts an existing decision, propose a correction where
   that decision lives. Do not silently build against it.
4. When missing access, tooling, or a dependency makes the work impossible,
   record the evidence, what it prevents, and who owns the boundary. This is
   not a design choice.
5. Publish literal provisional interfaces early when others will build against
   them. State separately whether behavior, field names, paths, and envelopes
   are settled.
6. A shipping restriction does not prevent a throwaway prototype. Keep such
   work outside the eventual repository, commit nothing, and record what the
   experiment established.
7. After the end-to-end run, classify every stand-in and unspecified choice as
   either a decision taken with rationale or an unresolved question with its
   consequence.
8. Stop when further work would only refine a known stage, when no defensible
   provisional choice can unlock dependent work, or when the next decision
   belongs across an ownership boundary. Record which condition stopped the
   work.

Research, record, and decision pillars may have no build loop. For them,
completion means the material is complete, accurate, current, and useful.
A thin screen for a building pillar usually means the work has not yet reached
the unknown; a thin screen for a non-building pillar can be correct.

## What each screen says

Use these sections in this order when they contain useful information:

1. **Objective and scope.** State the purpose, the completion condition, and
   what the pillar does not own.
2. **The work.** Give each necessary unit a plain-language headline and a
   summary that stands alone. Put code identifiers and exact evidence only in
   expanded detail.
3. **Decisions.** State the fork, the chosen option, the reason, and what would
   fail if the other option proves correct.
4. **Open questions.** State what remains unknown, what it prevents, and what
   evidence or decision would settle it.
5. **Where it stands.** State what is complete, what remains, and what cannot
   proceed.

Include an item only when it changes a deliverable, requirement, decision,
problem assessment, or the reader's understanding of what is complete or
unknown. Effort alone does not make an item relevant. Reassess old items
against the current work and remove setup details once they no longer change a
decision. Keep the screen at a length a person will read.

## Mission controller

The mission controller coordinates and does not perform pillar work. It owns
the mission overview, tracks every pillar, decides shared names and interface
shapes, and resolves decisions crossing pillar boundaries. Pillars propose;
the controller decides.

The controller reviews every pillar revision for duplication with another
pillar, relevance to the mission, and readability by someone other than its
author. It sends the result to the pillar and records the same judgment in the
mission signpost so later pillars can act on it.

### Write a mission check-in

Write a check-in after a coherent body of work lands, not after every commit or
pillar report:

```bash
graph journal write "Mission name check-in — N commits, largest advance in <area>" \
  --normal /tmp/normal.md --expanded /tmp/expanded.md \
  --start <ISO> --end <ISO> --type mixed
```

Use exact start and end timestamps. The headline must stand alone. The normal
body states what happened. The expanded body uses exactly these sections:

```text
N commits landed. Largest advance in <area> — <why it mattered>.

## Live UI Changes
- What the operator can now see and use.
## New Platform Functionality
- Named capabilities that stand without a ticket.
## Plans Changed
- What changed and what moved because of it.
## Blockers Encountered
- What is blocked, why, and who owns it.
## New Beads Written
- The count grouped by work, not a title list.
```

Report completed, verified facts in technical manual language. Do not include
process narration, disclaimers, or an uncertain commit count.

### Raise a mission-wide blocker

Only the mission controller writes the single Activity notification that
states what the whole mission needs from the operator. A pillar raises its
specific decision to the controller instead.

Write one `dashboard.activity.ask` member keyed by the controller session. The
row replaces the previous row rather than forming a feed. Its `compact`,
`normal`, and `expanded` values express one request at three depths; `compact`
must stand alone. Increase `revision_seq` on every rewrite. Remove the row when
the mission needs nothing.

The payload contains `session_id`, `compact`, `normal`, `expanded`,
`created_at`, and `revision_seq`.

An operator refresh request clears only after `revision_seq` advances, so each
rewrite must increase it even when the request remains active.

```bash
graph set add dashboard.activity.ask#2 --key "$AUTONOMY_SESSION" --from request.json
```

## Complete API reference

The common workflows above explain the routes that require behavioral
context. The remaining route surface is listed here so coordinators do not
invent database or Settings workarounds.

```text
GET    /api/missions
POST   /api/missions
GET    /api/missions/<id>
DELETE /api/missions/<id>                               operator authority
POST   /api/missions/<id>/status
POST   /api/missions/<id>/seen
POST   /api/missions/<id>/coordinator
POST   /api/missions/<id>/org

POST   /api/missions/<id>/style
GET    /api/missions/<id>/items
PUT    /api/missions/<id>/items/<item_id>
POST   /api/missions/<id>/items/<item_id>/state
GET    /api/pillars/<id>/items
PUT    /api/pillars/<id>/items/<item_id>
POST   /api/pillars/<id>/items/<item_id>/state

POST   /api/missions/<id>/site
GET    /api/missions/<id>/site
GET    /api/missions/<id>/site/revisions
GET    /api/missions/<id>/site/revisions/<revision_id>
POST   /api/missions/<id>/site/revisions/<revision_id>/activate
GET    /missions/<id>

POST   /api/missions/<id>/pillars
GET    /api/missions/<id>/pillars
GET    /api/pillars/<id>
DELETE /api/pillars/<id>                                operator authority
POST   /api/pillars/<id>/status
POST   /api/pillars/<id>/seen
POST   /api/pillars/<id>/coordinator
POST   /api/pillars/<id>/last-done
POST   /api/pillars/<id>/site
GET    /api/pillars/<id>/site
GET    /api/pillars/<id>/site/revisions
GET    /api/pillars/<id>/site/revisions/<revision_id>
POST   /api/pillars/<id>/site/revisions/<revision_id>/activate
GET    /missions/<mission_id>/pillars/<pillar_id>

GET    /api/missions/<id>/questions
POST   /api/missions/<id>/questions
POST   /api/missions/<id>/questions/<entry_id>/answer
POST   /api/missions/<id>/questions/<entry_id>/rephrase
POST   /api/missions/<id>/questions/<entry_id>/followup
POST   /api/missions/<id>/questions/<entry_id>/close
POST   /api/missions/<id>/questions/<entry_id>/update
POST   /api/missions/<id>/questions/<entry_id>/reopen
POST   /api/missions/<id>/asked
GET    /api/pillars/<id>/questions
POST   /api/pillars/<id>/questions
POST   /api/pillars/<id>/questions/<entry_id>/answer
POST   /api/pillars/<id>/questions/<entry_id>/rephrase
POST   /api/pillars/<id>/questions/<entry_id>/followup
POST   /api/pillars/<id>/questions/<entry_id>/close
POST   /api/pillars/<id>/questions/<entry_id>/update
POST   /api/pillars/<id>/questions/<entry_id>/reopen
POST   /api/pillars/<id>/asked
POST   /api/questions/<entry_id>/anchor
POST   /api/questions/<entry_id>/retire

GET    /api/missions/<id>/decision-log
GET    /api/missions/<id>/status-feed
POST   /api/missions/<id>/here
POST   /api/pillars/<id>/here

POST   /api/visitor-tokens                              operator authority
GET    /api/visitor-tokens/<participant_id>
POST   /api/visitor-tokens/<participant_id>/avatar
DELETE /api/visitor-tokens/<participant_id>
DELETE /api/presence/<surface_id>/<participant_id>
```

## Maintaining this document

Keep this file limited to current infrastructure, workflow, and artifact
requirements. Do not add product history, transition instructions, future
plans, or development phases. Replace superseded guidance instead of
accumulating it, and keep the shortest wording that still states every
behavior a coordinator must know.
