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
      // data-mc-* marks the destination declaratively. On the dashboard the
      // onclick navigates as usual; over the relay the viewer reads the
      // attribute and swaps in place. Without the marker the viewer would
      // have to guess a destination the handler builds by concatenation --
      // it cannot, and the entry silently does nothing.
      top.setAttribute("data-mc-home", "1");
      top.onclick = function () { window.location.href = "/missions/" + MISSION_ID; };
      menu.appendChild(top);
      pillars.forEach(function (p) {
        var row = el("div", { style: "padding:8px 10px;cursor:pointer;display:flex;align-items:center;gap:6px;" });
        var dot = el("span", { style: "width:8px;height:8px;border-radius:9999px;background:" + (p.color || "#6b7280") + ";" });
        row.appendChild(dot);
        row.appendChild(document.createTextNode(p.name));
        row.setAttribute("data-mc-pillar", p.pillar_id);
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

## 10. The conversation widget — anchored Q&A, live, on your own page

The embeddable counterpart to the top-bar (§8): a self-contained, vanilla-JS
widget that turns any element you mark up into a live discussion thread —
ask a question, watch a progress update or the final answer arrive without
a page reload, reopen with a follow-up if the answer wasn't right. Verified
this session against a real coordinator's live production site (a real
Postgres schema table, a real guest identity, a real answer arriving via
server-sent events with zero manual refresh) — this is shipped, working
code, not a mockup.

**Same self-containment rule as §8, and the same reason.** Paste both
blocks below directly into your generated HTML, once, before `</body>`.
Do not reference them as external `/static/...` files.

**The one thing you add per artifact:** `data-mc-anchor="<short-id>"` on
whatever element should be discussable — a table cell, a diagram, a
paragraph. Put the anchor on an element that can safely hold extra inline
content (the widget appends a small badge inside it) — a `<td>`, not a
`<tr>` (a `<tr>` may only contain `<td>`/`<th>`, so anything else appended
there gets silently relocated by the browser's own HTML parser — this bit
the first draft of this widget and is worth not repeating):

```html
<td data-mc-anchor="table:component_catalog_oss_projects">
  Keyed by canonical version-less purl…
</td>
```

Everything else — the discuss badge, the panel, presence, live updates,
asking, reopening — is handled entirely by the pasted-in code. A mission
writes zero lines of API, presence, or rendering code of its own.

**What it does and does not do.** Guests can ask and reopen; only a
coordinator's own agent-side API call (§6/§8, unchanged) can answer — this
widget never exposes an answer path, matching the existing design that
answering is the responder's job, not a browser action. Presence is
per-mission/per-pillar surface only, not per-anchor: the panel shows who's
around on this pillar right now, not literally who's looking at this one
row — guests aren't heartbeat-tracked continuously the way coordinators
are (§7), and the widget doesn't pretend otherwise.

**Live updates ride the plain event bus, same-origin only.** This widget
talks directly to your own mission/pillar's API over `fetch`/`EventSource`
— it has no relay dependency and does not work for a guest with no network
path to the dashboard (see `graph://ce07a01f-faa` for the relay-served,
personalized-guest-link path, which is separate, later work). For today's
`?as=<token>` guests (§6) and anyone with direct access, this is the real
thing: `EventSource("/api/events")` filtered to `mission_control:conversation`
(carries the full entry, so no refetch is needed) and `setting.changed`
for `dashboard.surface.presence` (a nudge to re-poll, not a payload).

```html
<style>
/* Mission Control embeddable widget -- self-contained styles, mc- prefixed
   to avoid colliding with the host page's own CSS. Dark-theme colors
   chosen to read well against either a dark or light host page (small
   footprint, high-contrast panel, not a full theme takeover). */
#mc-panel-host{position:fixed;right:16px;bottom:16px;width:340px;max-width:calc(100vw - 32px);
  max-height:70vh;overflow-y:auto;z-index:9999;display:flex;flex-direction:column;gap:10px}
.mc-anchor-open{background:rgba(99,102,241,.08)!important;box-shadow:inset 3px 0 0 0 #6366f1}
.mc-badge{display:inline-flex;align-items:center;gap:4px;margin-left:8px;padding:2px 9px;
  border-radius:999px;border:none;cursor:pointer;font-size:11px;font-family:inherit;
  background:#30363d;color:#8b98ab;vertical-align:middle}
.mc-badge:hover{color:#dbe4f0}
.mc-badge-open{background:#4f46e5;color:#fff}
.mc-badge-pending:not(.mc-badge-open){background:#78350f;color:#fcd34d}
.mc-badge-resolved:not(.mc-badge-open){background:#312e81;color:#a5b4fc}
.mc-panel{margin:6px 0 18px;padding:12px 14px;border-radius:8px;background:#0d1117;
  border:1px solid #30363d;border-left:3px solid #6366f1;font-size:13px;color:#dbe4f0;
  font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
.mc-panel-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.mc-discussing{font-size:11px;color:#a5b4fc}
.mc-presence{display:flex;align-items:center;gap:4px}
.mc-avatar{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;
  border-radius:999px;font-size:9px;font-weight:700;color:#0b0f19;flex-shrink:0}
.mc-presence-label{font-size:11px;color:#8b98ab;margin-left:2px}
.mc-empty{color:#6b7684;font-size:12.5px;margin:4px 0}
.mc-message{display:flex;gap:8px;margin:8px 0}
.mc-message-body{min-width:0;flex:1}
.mc-message-meta{display:flex;align-items:baseline;gap:6px}
.mc-name{font-size:12px;font-weight:600;color:#dbe4f0}
.mc-when{font-size:10px;color:#57606a}
.mc-text{font-size:12.5px;color:#c7d4e8;margin-top:2px;line-height:1.5}
.mc-update{display:flex;align-items:center;gap:6px;padding-left:26px;margin:4px 0}
.mc-update-dot{width:4px;height:4px;border-radius:999px;background:#57606a;flex-shrink:0}
.mc-update-text{font-size:11px;color:#8b98ab;font-style:italic}
.mc-waiting{display:flex;align-items:center;gap:4px;padding-left:26px;margin:6px 0}
.mc-dot{width:4px;height:4px;border-radius:999px;background:#57606a;animation:mc-blink 1.2s infinite}
.mc-dot:nth-child(2){animation-delay:.2s}
.mc-dot:nth-child(3){animation-delay:.4s}
@keyframes mc-blink{0%,80%,100%{opacity:.2}40%{opacity:1}}
.mc-waiting-label{font-size:11px;color:#57606a;margin-left:4px}
.mc-followup-link{background:none;border:none;color:#818cf8;font-size:11.5px;cursor:pointer;
  padding:0;margin-top:4px;font-family:inherit}
.mc-followup-link:hover{color:#a5b4fc;text-decoration:underline}
.mc-input{flex:1;background:#161b22;border:1px solid #30363d;border-radius:999px;
  padding:6px 12px;font-size:12.5px;color:#dbe4f0;font-family:inherit}
.mc-input:focus{outline:none;border-color:#6366f1}
.mc-error{width:100%;font-size:11px;color:#f85149;margin-bottom:2px}
.mc-compose-wrap{display:flex;align-items:center;gap:6px;margin-top:8px;flex-wrap:wrap}
.mc-send{flex-shrink:0;width:26px;height:26px;border-radius:999px;border:none;background:#4f46e5;
  color:#fff;cursor:pointer;font-size:12px;line-height:1}
.mc-send:hover{background:#4338ca}
.mc-send:disabled,.mc-input:disabled{opacity:.5}
</style>
<script>
(function () {
  "use strict";
  // ── Mission Control embeddable conversation widget ──────────────────
  // Paste this block once, before </body>. Mark any element you want
  // discussable with data-mc-anchor="<short-id>" -- everything else
  // (API calls, presence, rendering) is handled here. No external
  // script references -- self-contained by design, see SKILL.md §10.
  // Vanilla JS on purpose: this widget must drop into any coordinator's
  // page regardless of that page's own stack (this site is vanilla; the
  // widget doesn't require Alpine or any other framework to be present).
  var API_BASE = "";              // fill in at build time, e.g. "https://localhost:8080"
  var MISSION_ID = "";            // fill in at build time
  var CURRENT_PILLAR_ID = "";     // fill in at build time; "" = mission-level, not a pillar

  var QUESTIONS_BASE = CURRENT_PILLAR_ID
    ? API_BASE + "/api/pillars/" + CURRENT_PILLAR_ID + "/questions"
    : API_BASE + "/api/missions/" + MISSION_ID + "/questions";
  var SURFACE_ID = CURRENT_PILLAR_ID ? "pillar:" + CURRENT_PILLAR_ID : "mission:" + MISSION_ID;

  // ── tiny DOM helper, no dependencies ─────────────────────────────
  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) {
      if (k === "class") e.className = attrs[k];
      else if (k === "html") { /* never used for API-sourced text -- see textContent below */ }
      else e.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  }
  function text(s) { return document.createTextNode(s == null ? "" : String(s)); }
  function fmtWhen(epochSeconds) {
    try {
      var s = Math.round(Date.now() / 1000 - epochSeconds);
      if (s < 5) return "just now";
      if (s < 60) return s + "s ago";
      if (s < 3600) return Math.round(s / 60) + "m ago";
      if (s < 86400) return Math.round(s / 3600) + "h ago";
      return Math.round(s / 86400) + "d ago";
    } catch (e) { return ""; }
  }

  // ── presence read -- no external script, see graph://ce07a01f-faa ──
  // Polled every 10s (matches the write-side heartbeat interval), plus
  // refreshed immediately on a "setting.changed" SSE nudge for this
  // set_id. No per-anchor granularity: presence is per mission/pillar
  // surface only -- guests aren't heartbeat-tracked continuously, only
  // coordinators, and only on push/answer. Don't overpromise more than
  // that in the UI.
  function mcFetchPresence(surfaceId) {
    return fetch(API_BASE + "/api/graph/settings/dashboard.surface.presence", {
      headers: { "X-Graph-Org": "autonomy" },
    })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        return (body.members || [])
          .map(function (m) { return m.payload; })
          .filter(function (p) { return p.surface_id === surfaceId; });
      })
      .catch(function () { return []; });
  }
  function mcColor(id) {
    var h = 0;
    for (var i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
    return "hsl(" + (h % 360) + " 60% 55%)";
  }
  function mcInitial(label) {
    return (label || "?").trim().charAt(0).toUpperCase() || "?";
  }

  var presenceListeners = [];
  function pollPresence() {
    mcFetchPresence(SURFACE_ID).then(function (participants) {
      presenceListeners.forEach(function (fn) { fn(participants); });
    });
  }
  setInterval(pollPresence, 10000);

  var _panelHost = null;
  function mcPanelHost() {
    if (!_panelHost) {
      _panelHost = el("div", { id: "mc-panel-host" });
      document.body.appendChild(_panelHost);
    }
    return _panelHost;
  }

  // ── one row per anchor: state + rendering + actions ──────────────
  function Row(hostEl, anchor) {
    this.host = hostEl;
    this.anchor = anchor;
    this.open = false;
    this.entry = null;
    this.followingUp = false;
    this.presence = [];
    this._buildChrome();
    var self = this;
    presenceListeners.push(function (p) {
      self.presence = p;
      if (self.open) self._renderPresence();
    });
  }

  Row.prototype._buildChrome = function () {
    var self = this;
    this.badge = el("button", { type: "button", class: "mc-badge", "data-mc-anchor": this.anchor });
    this.badge.textContent = "discuss";
    this.badge.addEventListener("click", function () { self.toggle(); });
    // Appended INSIDE the anchor element, never as its sibling: the
    // anchor can be anything a coordinator marks up, including a <td>,
    // where a <tr>-level sibling insert would be invalid HTML (a <tr>
    // may only contain <td>/<th>) and get silently relocated by the
    // browser's parser. A trailing inline badge is valid inside any
    // anchor element that can hold text/inline content.
    this.host.appendChild(this.badge);

    this.panel = el("div", { class: "mc-panel", style: "display:none" });
    this.headerRow = el("div", { class: "mc-panel-header" });
    this.presenceRow = el("div", { class: "mc-presence" });
    this.headerRow.appendChild(el("span", { class: "mc-discussing" }, [text("Discussing " + this.anchor)]));
    this.headerRow.appendChild(this.presenceRow);
    this.body = el("div", { class: "mc-body" });
    this.composeWrap = el("div", { class: "mc-compose-wrap" });
    this.panel.appendChild(this.headerRow);
    this.panel.appendChild(this.body);
    this.panel.appendChild(this.composeWrap);
    // Panels all live in ONE shared container at the end of <body>, not
    // as a DOM sibling of the anchor -- the anchor can be any element
    // type (a table cell, a span, a div), and a block-level panel isn't
    // valid next to/inside all of them (again, <tr>/<td> being the
    // sharpest example). The badge already ties the panel to its anchor
    // visually via the "Discussing <anchor>" label; DOM adjacency isn't
    // required for that connection to read clearly.
    mcPanelHost().appendChild(this.panel);
  };

  Row.prototype.toggle = function () {
    this.open = !this.open;
    this.panel.style.display = this.open ? "" : "none";
    // Visual tie-back to the anchor now that the panel isn't a DOM
    // sibling of it -- safe on any element type (a <td> included).
    if (this.host.classList) this.host.classList.toggle("mc-anchor-open", this.open);
    this._updateBadge();
    if (this.open) {
      pollPresence();
      this.refresh();
    }
  };

  Row.prototype._updateBadge = function () {
    this.badge.className = "mc-badge" + (this.open ? " mc-badge-open" : "")
      + (this.entry ? (this.entry.answer ? " mc-badge-resolved" : " mc-badge-pending") : "");
    this.badge.textContent = this.open
      ? (this.entry ? (this.entry.answer ? "resolved" : "open") : "discuss")
      : (this.entry ? (this.entry.answer ? "resolved" : "open") : "discuss");
  };

  Row.prototype.refresh = function () {
    var self = this;
    return fetch(QUESTIONS_BASE, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        var entry = (body.questions || []).find(function (q) { return q.anchor === self.anchor; });
        self.entry = entry || null;
        self._updateBadge();
        self.render();
      });
  };

  Row.prototype._renderPresence = function () {
    this.presenceRow.textContent = "";
    var self = this;
    this.presence.slice(0, 5).forEach(function (p) {
      var av = el("span", { class: "mc-avatar", style: "background:" + mcColor(p.participant_id) });
      av.textContent = mcInitial(p.participant_label);
      self.presenceRow.appendChild(av);
    });
    var label = this.presence.length
      ? this.presence.length + " here now"
      : "no one else here right now";
    this.presenceRow.appendChild(el("span", { class: "mc-presence-label" }, [text(label)]));
  };

  Row.prototype.render = function () {
    this._renderPresence();
    this.body.textContent = "";
    this.composeWrap.textContent = "";

    if (!this.entry) {
      this.body.appendChild(el("p", { class: "mc-empty" }, [text("No discussion yet.")]));
      this._renderCompose(false);
      return;
    }

    var q = this.entry;
    var askedRow = el("div", { class: "mc-message" });
    askedRow.appendChild(el("span", { class: "mc-avatar", style: "background:" + mcColor(q.asked_by_participant_id) }, [text(mcInitial(q.asked_by_label))]));
    var askedBody = el("div", { class: "mc-message-body" });
    var askedMeta = el("div", { class: "mc-message-meta" });
    askedMeta.appendChild(el("span", { class: "mc-name" }, [text(q.asked_by_label)]));
    askedMeta.appendChild(el("span", { class: "mc-when" }, [text(fmtWhen(q.created_at))]));
    askedBody.appendChild(askedMeta);
    askedBody.appendChild(el("div", { class: "mc-text" }, [text(q.question)]));
    askedRow.appendChild(askedBody);
    this.body.appendChild(askedRow);

    if (!q.answer) {
      // Ephemeral, only while open -- vanishes from the record the
      // instant an answer lands (server already drops `updates` once
      // answer IS NOT NULL; this just mirrors that, doesn't re-decide it).
      (q.updates || []).forEach(function (u) {
        var uRow = el("div", { class: "mc-update" });
        uRow.appendChild(el("span", { class: "mc-update-dot" }));
        uRow.appendChild(el("span", { class: "mc-update-text" }, [text(u.text)]));
        this.body.appendChild(uRow);
      }, this);
      var waiting = el("div", { class: "mc-waiting" });
      waiting.appendChild(el("span", { class: "mc-dot" }));
      waiting.appendChild(el("span", { class: "mc-dot" }));
      waiting.appendChild(el("span", { class: "mc-dot" }));
      waiting.appendChild(el("span", { class: "mc-waiting-label" }, [text("waiting for a reply")]));
      this.body.appendChild(waiting);
      this._renderCompose(false);
      return;
    }

    var ansRow = el("div", { class: "mc-message" });
    ansRow.appendChild(el("span", { class: "mc-avatar", style: "background:" + mcColor("responder") }, [text("A")]));
    var ansBody = el("div", { class: "mc-message-body" });
    var ansMeta = el("div", { class: "mc-message-meta" });
    ansMeta.appendChild(el("span", { class: "mc-name" }, [text("auto-schema")]));
    ansMeta.appendChild(el("span", { class: "mc-when" }, [text(fmtWhen(q.answered_at))]));
    ansBody.appendChild(ansMeta);
    ansBody.appendChild(el("div", { class: "mc-text" }, [text(q.answer)]));
    ansRow.appendChild(ansBody);
    this.body.appendChild(ansRow);
    this._renderCompose(true);
  };

  Row.prototype._renderCompose = function (answered) {
    var self = this;
    if (answered && !this.followingUp) {
      var link = el("button", { type: "button", class: "mc-followup-link" });
      link.textContent = "Not quite — ask a follow-up";
      link.addEventListener("click", function () { self.followingUp = true; self.render(); });
      this.composeWrap.appendChild(link);
      return;
    }
    var input = el("input", {
      type: "text",
      class: "mc-input",
      placeholder: answered ? "What's still unclear…" : "Ask a question…",
    });
    var send = el("button", { type: "button", class: "mc-send" });
    send.textContent = "➤";
    function showError(msg) {
      var err = self.composeWrap.querySelector(".mc-error");
      if (!err) {
        err = el("div", { class: "mc-error" });
        self.composeWrap.insertBefore(err, self.composeWrap.firstChild);
      }
      err.textContent = msg;
    }
    function doSend() {
      var val = input.value.trim();
      if (!val) return;
      input.disabled = true; send.disabled = true;
      var req = answered
        ? fetch(QUESTIONS_BASE + "/" + self.entry.entry_id + "/reopen" + mcAsParam(), {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ followup: val }),
          })
        : fetch(QUESTIONS_BASE + mcAsParam(), {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: val, anchor: self.anchor }),
          });
      req.then(function (r) {
        if (r.status === 401) { showError("This link needs to be reopened from your invite to post — try it again from your original link."); throw new Error("unauthorized"); }
        if (!r.ok) { showError("That didn't go through — try again in a moment."); throw new Error("request failed"); }
        return r.json();
      }).then(function () {
        self.followingUp = false;
        input.disabled = false; send.disabled = false;
        self.refresh();
      }).catch(function () {
        input.disabled = false; send.disabled = false;
      });
    }
    input.addEventListener("keydown", function (ev) { if (ev.key === "Enter") doSend(); });
    send.addEventListener("click", doSend);
    this.composeWrap.appendChild(input);
    this.composeWrap.appendChild(send);
  };

  function mcAsParam() {
    // The visitor token, if this page was opened with ?as=<token>, is
    // already carried by the mc_visitor cookie after first load -- see
    // SKILL.md §6. Nothing to add here; same-origin fetch sends cookies
    // automatically. Placeholder kept for a future explicit-token path.
    return "";
  }

  // ── live updates: ride the plain /api/events SSE bus, no relay ────
  // Same-origin/local access only (see graph://ce07a01f-faa for why the
  // relay-served case is a separate, not-yet-built path). Filters two
  // topics: mission_control:conversation (carries the full entry, no
  // refetch needed) and setting.changed for dashboard.surface.presence
  // (just a nudge -- presence is refetched, not carried in the event).
  function startLiveUpdates(rows) {
    var es;
    try {
      es = new EventSource(API_BASE + "/api/events");
    } catch (e) {
      return; // no live updates; rows still work via manual refresh on open
    }
    es.addEventListener("mission_control:conversation", function (ev) {
      var data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      if (data.mission_id !== MISSION_ID) return;
      if ((data.pillar_id || "") !== (CURRENT_PILLAR_ID || "")) return;
      rows.forEach(function (row) {
        if (row.open && row.entry && row.entry.entry_id === data.entry_id) row.refresh();
        else if (row.open && !row.entry && data.question && data.question.anchor === row.anchor) row.refresh();
      });
    });
    es.addEventListener("setting.changed", function (ev) {
      var data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      if (data.set_id === "dashboard.surface.presence") pollPresence();
    });
  }

  // ── boot: scan for anchors, wire everything ────────────────────────
  document.addEventListener("DOMContentLoaded", function () {
    var rows = [];
    document.querySelectorAll("[data-mc-anchor]").forEach(function (hostEl) {
      var anchor = hostEl.getAttribute("data-mc-anchor");
      if (!anchor) return;
      rows.push(new Row(hostEl, anchor));
    });
    startLiveUpdates(rows);
    pollPresence();
  });
})();
</script>
```

**Verified this session:** pushed onto a real pillar site built from the OSS
Insights coordinator's actual production HTML (their real `component_catalog`
schema table, unmodified except for three `data-mc-anchor` attributes),
opened via a real minted guest identity — a question asked, answered from a
separate curl call standing in for the pillar's own coordinator session, and
the answer arriving in the open browser tab with no manual reload, purely
over the SSE mechanism above. Screenshots: `graph://89fb0c04-017`.
