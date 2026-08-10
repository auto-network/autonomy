# Mission Control — pushing a mission site from an agent session

Mission Control (`/mission-control`) hosts chromeless native sites, one per
mission, with immutable revision history. P1: missions + site hosting. P2
(§6): visitor Q&A attribution, including reopening an answered question for
a follow-up round. P3 (§7): presence and "what changed since you last
looked." P4 (§8): pillars — a mission split into dedicated sub-coordinators,
each with its own site, presence surface, and anchored conversation. §9:
how to actually write what goes in the record.

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
created_at}`. `coordinator_session` is plain data, not an invariant: it isn't
hard-bound anywhere in P1 and can go stale if the coordinator session is
replaced. There is no PATCH for it yet; that lands with P2 (the Q&A relay
reads it at message time).

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

Chromeless — raw HTML, no dashboard furniture, stable across every future
push. The response is marked uncacheable end to end
(`Cache-Control: no-store, no-cache, must-revalidate, max-age=0`) and always
reads the current revision fresh from storage: a push is visible on the next
request, with no caching window to wait out.

Your HTML is served as-is, unlike Present's viewer — there is **no iframe
rebuild, no injected Tailwind/Alpine CDN, no scroll-snap slide chopping**.
Ship a complete, self-contained document (`<!doctype html>` through
`</html>`) exactly as you want it rendered.

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

## 6. Q&A — visitors ask, you answer (P2)

A person viewing the mission site can ask a question; you get notified over
CrossTalk; you answer via the API; the question + your final answer become
part of the mission's permanent conversation history. P2 scope is
attribution only — no live presence integration yet.

### Mint a share link for a person (you do this, once per person)

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

## 7. Presence and "what changed" (P3)

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

## 8. Pillars — sub-missions with their own coordinator (P4)

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
GET    /missions/<mission_id>/pillars/<pillar_id>                  # chromeless direct-serve
```

`GET /missions/<mission_id>/pillars/<pillar_id>` is a convenience URL for
bookmarking/refreshing on one pillar once you're already inside a mission —
**it is never the link you hand out**. See "one link per mission" below.

### One link per mission, not one per pillar

The share link you mint (§6) and hand to a person is always the
**mission-level** link. A guest navigates to a specific pillar from inside
the mission's own page — your top-level HTML fetches
`GET /api/missions/<id>/pillars` and links or embeds pillar content
client-side. There is no separate onboarding step per pillar.

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
coordinator only, unchanged from P2. Your CrossTalk envelope tells you
which role you're in.

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

### The mission top-bar — a reusable, inline-able navigation widget

A small, mobile-first, sticky bar for your **mission-level** HTML (the page
the one share link points to): shows who's currently around and lets a
visitor jump between pillars without you hand-rolling a nav component.
**Inline this snippet directly into your generated HTML** — do not
reference it as an external `/static/...` script. Mission sites are
meant to stay self-contained (some have survived multiple hosting
migrations specifically because of this property); a future viewer path
(auto.network relay-served bytes) may not serve platform static assets
alongside your page at all, so a same-origin script tag is a real risk, not
just a style preference.

```html
<div id="mc-topbar" style="position:sticky;top:0;z-index:40;display:flex;
     align-items:center;justify-content:space-between;gap:8px;padding:8px 12px;
     background:#12172380;backdrop-filter:blur(6px);border-bottom:1px solid #1f2937;
     font:12px/1.4 system-ui,sans-serif;color:#e5e7eb;">
  <div id="mc-topbar-switcher" style="display:flex;align-items:center;gap:6px;cursor:pointer;">
    <span id="mc-topbar-label">Loading…</span>
    <span style="color:#6b7280;">▾</span>
  </div>
  <div id="mc-topbar-presence" style="display:flex;align-items:center;gap:-6px;"></div>
</div>
<div id="mc-topbar-menu" style="display:none;position:fixed;top:44px;left:8px;right:8px;
     max-width:280px;background:#12172a;border:1px solid #1f2937;border-radius:8px;
     padding:4px;z-index:41;"></div>
<script>
(function () {
  var MISSION_ID = "<mission_id>";        // fill in at build time
  var CURRENT_PILLAR_ID = null;           // set to a pillar_id on that pillar's own page, else null
  var API_BASE = "";                      // same origin as this page

  function el(tag, attrs) {
    var e = document.createElement(tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  fetch(API_BASE + "/api/missions/" + MISSION_ID + "/pillars")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var pillars = (data && data.pillars) || [];
      var current = pillars.find(function (p) { return p.pillar_id === CURRENT_PILLAR_ID; });
      document.getElementById("mc-topbar-label").textContent =
        current ? current.name : "Mission overview";

      var menu = document.getElementById("mc-topbar-menu");
      var top = el("div", { style: "padding:8px 10px;cursor:pointer;color:#a5b4fc;" });
      top.textContent = "← Mission overview";
      top.onclick = function () { window.location.href = "/missions/" + MISSION_ID; };
      menu.appendChild(top);
      pillars.forEach(function (p) {
        var row = el("div", { style: "padding:8px 10px;cursor:pointer;display:flex;align-items:center;gap:6px;" });
        var dot = el("span", { style: "width:8px;height:8px;border-radius:9999px;background:" + (p.color || "#6b7280") + ";" });
        row.appendChild(dot);
        row.appendChild(document.createTextNode(p.name));
        row.onclick = function () { window.location.href = "/missions/" + MISSION_ID + "/pillars/" + p.pillar_id; };
        menu.appendChild(row);
      });
      document.getElementById("mc-topbar-switcher").onclick = function () {
        menu.style.display = menu.style.display === "none" ? "block" : "none";
      };

      // Presence: same-origin surface-presence.js IS safe to reference --
      // it's not your own content, it's the platform substrate every
      // dashboard page already loads; only YOUR page's own asset
      // self-containment is the concern this snippet is designed around.
      var surfaceId = CURRENT_PILLAR_ID ? "pillar:" + CURRENT_PILLAR_ID : "mission:" + MISSION_ID;
      if (window.Presence) {
        var state = Presence.alpine ? null : null; // Presence.alpine() is an Alpine wrapper;
        // outside an Alpine page, poll participants directly instead:
        fetch(API_BASE + "/api/graph/settings/dashboard.surface.presence%231")
          .catch(function () {}); // best-effort; wire to your own polling/render if you want live avatars
      }
    })
    .catch(function () {
      document.getElementById("mc-topbar-label").textContent = "Mission Control";
    });
})();
</script>
```

The presence half is deliberately left as a stub above — `Presence.alpine()`
is an Alpine.js-specific wrapper; if your site doesn't run Alpine, poll
the surface directly or ask for a plain-JS presence reader when you build
against this — flag it as a gap if you need it, don't reverse-engineer
`surface-presence.js` yourself.

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

**Why this matters more here than it might elsewhere:** the decision log
(§8) is computed straight from these fields — there is no separate
editorial pass that cleans them up before anyone reads them. Whatever you
write is, verbatim, what lands in the permanent cross-pillar record. Write
the version you'd want to read cold, six months from now, with no memory
of the conversation that produced it.
