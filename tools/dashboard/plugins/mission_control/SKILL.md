# Mission Control — pushing a mission site from an agent session

Mission Control (`/mission-control`) hosts chromeless native sites, one per
mission, with immutable revision history. P1 scope: missions + site hosting
only. Resources, Q&A, and live data feeds arrive with their own phases —
if you're looking for those, they aren't here yet.

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
```

No `GET .../status` vs `.../full` split, no membership-set indirection, no
`graph set remove` needed for deletion — `DELETE /api/missions/<id>` is a
real route.

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
