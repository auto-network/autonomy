"""Mission Control backend API.

P1: missions + native site hosting. Entity model is deliberately
minimal — a mission is ``{mission_id, name, coordinator_session,
created_at, current_revision_id, status}``. Resources and live data feeds
arrive with their own phases and are not guessed at here.

Storage is Mission Control's own (``tools.dashboard.dao.mission_control_db``),
not a foreign key into Design Studio's design/revision tables — see that
module's docstring for why. A push to a mission's site both stores AND
publishes in one call; there is no separate "mark shown" step.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from tools.dashboard.dao import mission_control_db as db
from tools.dashboard.plugins.mission_control import compose
from tools.dashboard.event_bus import event_bus

logger = logging.getLogger(__name__)

#: Topic for every conversation write (ask/answer/reopen/update) -- one
#: topic, not one per event kind, matching the existing "setting.changed"
#: convention of a small number of topics distinguished by payload fields
#: rather than a proliferating topic namespace. A live viewer subscribes
#: once and filters client-side on mission_id/pillar_id, the same "one
#: unfiltered subscription plus a comparison" shape already settled for
#: live session sharing (graph://248d2e36-4cc).
MISSION_CONVERSATION_TOPIC = "mission_control:conversation"


async def _publish_conversation_event(
    event: str, mission_id: str, pillar_id: str | None, entry_id: str,
    *, question: dict | None = None, update: dict | None = None,
) -> None:
    """Best-effort live-update publish -- never blocks or fails the
    caller's response. The payload already carries the parsed entry (the
    caller already built it for the HTTP response), so a subscriber never
    needs a refetch to render it -- same principle the live-session-sharing
    design settled on for its own connector (graph://248d2e36-4cc).
    """
    try:
        await event_bus.broadcast(MISSION_CONVERSATION_TOPIC, {
            "event": event,
            "mission_id": mission_id,
            "pillar_id": pillar_id,
            "entry_id": entry_id,
            "question": question,
            "update": update,
        }, dedup=False)
    except Exception:
        logger.warning(
            "mission_control conversation event publish failed for %s/%s",
            mission_id, entry_id, exc_info=True,
        )

#: Cookie carrying the visitor's bearer TOKEN (never the display-safe
#: participant_id -- see resolve_visitor()'s docstring for why that
#: distinction is load-bearing, not cosmetic). HttpOnly: JS never needs
#: to read it, only the browser needs to send it back automatically.
VISITOR_COOKIE = "mc_visitor"
VISITOR_COOKIE_MAX_AGE = 365 * 24 * 3600


def _mission_payload(mission: dict) -> dict:
    return {
        "mission_id": mission["mission_id"],
        "name": mission["name"],
        "coordinator_session": mission["coordinator_session"],
        "created_at": mission["created_at"],
        "current_revision_id": mission["current_revision_id"],
        "status": mission["status"],
    }


def _revision_payload(revision: dict, *, include_html: bool) -> dict:
    """Shared by both mission_site_revisions rows (carry mission_id) and
    pillar_site_revisions rows (carry pillar_id) -- only one of the two
    keys is ever present on a given row, never both."""
    payload = {
        "revision_id": revision["revision_id"],
        "revision_seq": revision["revision_seq"],
        "note": revision["note"],
        "created_at": revision["created_at"],
    }
    if "mission_id" in revision:
        payload["mission_id"] = revision["mission_id"]
    if "pillar_id" in revision:
        payload["pillar_id"] = revision["pillar_id"]
    if include_html:
        payload["html"] = revision["html"]
    else:
        payload["byte_size"] = revision.get("byte_size")
    return payload


async def list_missions(request: Request) -> JSONResponse:
    missions = [_mission_payload(m) for m in db.list_missions()]
    return JSONResponse({"missions": missions})


async def create_mission(request: Request) -> JSONResponse:
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    coordinator_session = (body.get("coordinator_session") or "").strip()
    mission = db.create_mission(name, coordinator_session)
    return JSONResponse({"mission": _mission_payload(mission)}, status_code=201)


async def get_mission(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    mission = db.get_mission(mission_id)
    if not mission:
        return JSONResponse({"error": "mission not found"}, status_code=404)
    payload = _mission_payload(mission)
    if mission["current_revision_id"]:
        current = db.get_current_site(mission_id)
        if current:
            payload["current_revision"] = _revision_payload(current, include_html=False)
    payload["open_question_count"] = db.count_open_questions(mission_id)
    last_seen = db.get_last_seen(mission_id)
    if last_seen is None:
        # Never seen before: baseline is established by the first POST
        # .../seen, not "dump the whole history as new" -- that would be
        # noisy and misleading on a mission's very first view.
        payload["since_last_visit"] = {"last_seen_at": None, "revisions": [], "questions": []}
    else:
        payload["since_last_visit"] = {
            "last_seen_at": last_seen,
            "revisions": [
                _revision_payload(r, include_html=False)
                for r in db.list_site_revisions_since(mission_id, last_seen)
            ],
            "questions": [
                _question_payload(q)
                for q in db.list_conversation_since(mission_id, last_seen)
            ],
        }
    return JSONResponse({"mission": payload})


async def mark_mission_seen(request: Request) -> JSONResponse:
    """Advance a mission's last-seen watermark. A deliberate action, called
    when a viewer opens a mission's detail panel -- never a side effect of
    the incidental GET .../missions/<id> the list page fires just to
    hydrate summary fields (see get_mission's since_last_visit comment)."""
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    seen_at = db.mark_seen(mission_id)
    return JSONResponse({"ok": True, "seen_at": seen_at})


async def set_mission_status(request: Request) -> JSONResponse:
    """Coordinator-set lifecycle state -- never inferred from staleness
    (see mission_control_db.VALID_MISSION_STATUSES)."""
    mission_id = request.path_params["mission_id"]
    body = await request.json()
    status = body.get("status")
    if status not in db.VALID_MISSION_STATUSES:
        return JSONResponse(
            {"error": f"status must be one of {db.VALID_MISSION_STATUSES}"},
            status_code=400,
        )
    ok = db.set_mission_status(mission_id, status)
    if not ok:
        return JSONResponse({"error": "mission not found"}, status_code=404)
    return JSONResponse({"mission": _mission_payload(db.get_mission(mission_id))})


async def delete_mission(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    deleted = db.delete_mission(mission_id)
    if not deleted:
        return JSONResponse({"error": "mission not found"}, status_code=404)
    return JSONResponse({"ok": True})


def _heartbeat_presence(surface_id: str, participant_id: str,
                        label: str = "", kind: str = "agent") -> None:
    """Record that someone is on this surface right now. Never raises."""
    if not participant_id:
        return
    try:
        from tools.graph.surface import Presence
        with Presence(
            surface_id=surface_id, participant_kind=kind,
            participant_id=participant_id, label=label or participant_id,
            org=compose.PRESENCE_ORG,
        ):
            pass
    except Exception:
        logger.warning("presence heartbeat failed for surface=%s participant=%s",
                       surface_id, participant_id, exc_info=True)


def _surface_presence(surface_id: str) -> list[dict]:
    """Everyone currently on one surface, mission or pillar."""
    from tools.dashboard.plugins.mission_control import compose as _c
    return _c._presence(surface_id, time.time())


def _heartbeat_coordinator_presence(surface_id: str, coordinator_session: str) -> None:
    """Best-effort presence touch for whoever is coordinating this mission
    or pillar. surface_id is "mission:<id>" or "pillar:<id>" -- the two
    presence surfaces this plugin writes.

    A coordinator pushing a revision or answering a question is
    unambiguously "working this mission right now" -- record it with zero
    coordinator-side integration required (suggested by the OSS Insights
    coordinator, auto-0709-092918, as a cheap partial fix for the "why
    isn't the coordinator in Presence" gap). Never fails or blocks the
    caller's response -- a presence hiccup must not break a push or an
    answer.
    """
    if not coordinator_session:
        return
    try:
        from tools.graph.surface import Presence
        with Presence(
            surface_id=surface_id,
            participant_kind="agent",
            participant_id=coordinator_session,
            label=coordinator_session,
            org=compose.PRESENCE_ORG,
        ):
            pass
    except Exception:
        logger.warning(
            "presence heartbeat failed for surface=%s coordinator=%s",
            surface_id, coordinator_session, exc_info=True,
        )


async def push_site_revision(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    body = await request.json()
    html = body.get("html")
    if not isinstance(html, str) or not html.strip():
        return JSONResponse({"error": "html is required"}, status_code=400)
    note = (body.get("note") or "").strip()
    revision = db.push_site_revision(mission_id, html, note)
    if revision is None:
        return JSONResponse({"error": "mission not found"}, status_code=404)
    mission = db.get_mission(mission_id)
    _heartbeat_coordinator_presence(
        f"mission:{mission_id}", mission.get("coordinator_session") if mission else "",
    )
    return JSONResponse(
        {"revision": _revision_payload({**revision, "byte_size": len(html)}, include_html=False)},
        status_code=201,
    )


async def get_current_site(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    current = db.get_current_site(mission_id)
    if not current:
        return JSONResponse({"error": "mission has no site revision yet"}, status_code=404)
    return JSONResponse({"revision": _revision_payload(current, include_html=True)})


async def list_site_revisions(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    revisions = [
        _revision_payload(r, include_html=False)
        for r in db.list_site_revisions(mission_id)
    ]
    return JSONResponse({"revisions": revisions})


async def get_site_revision(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    revision_id = request.path_params["revision_id"]
    revision = db.get_site_revision(mission_id, revision_id)
    if not revision:
        return JSONResponse({"error": "revision not found"}, status_code=404)
    return JSONResponse({"revision": _revision_payload(revision, include_html=True)})


async def activate_site_revision(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    revision_id = request.path_params["revision_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    ok = db.activate_site_revision(mission_id, revision_id)
    if not ok:
        return JSONResponse({"error": "revision not found"}, status_code=404)
    current = db.get_current_site(mission_id)
    return JSONResponse({"revision": _revision_payload(current, include_html=False)})


# ── Pillars ──────────────────────────────────────────────────────
#
# A pillar is a sub-mission: its own coordinator_session, its own site-
# revision history, its own presence surface (pillar:<id>). Every handler
# here is a structural mirror of its mission-level counterpart above.


#: Two sentences of plain language. Generous enough that a legitimate one
#: never hits it, tight enough that a pasted status report does.
LAST_DONE_MAX = 400


def _pillar_payload(pillar: dict) -> dict:
    return {
        "pillar_id": pillar["pillar_id"],
        "mission_id": pillar["mission_id"],
        "name": pillar["name"],
        "coordinator_session": pillar["coordinator_session"],
        "color": pillar["color"],
        "created_at": pillar["created_at"],
        "current_revision_id": pillar["current_revision_id"],
        "status": pillar["status"],
        "last_done": pillar["last_done"],
        "last_done_at": pillar["last_done_at"],
    }


async def create_pillar(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    coordinator_session = (body.get("coordinator_session") or "").strip()
    color = (body.get("color") or "").strip()
    pillar = db.create_pillar(mission_id, name, coordinator_session, color)
    return JSONResponse({"pillar": _pillar_payload(pillar)}, status_code=201)


async def list_pillars(request: Request) -> JSONResponse:
    """Unlike list_missions (which the dashboard enriches per-mission via a
    GET .../missions/<id> round trip each, matching missions' small,
    coarse-grained N), a pillar grid can show several pillars per mission
    -- N+1-fetching each pillar's own detail from the client would multiply
    across missions x pillars. open_question_count is enriched here,
    server-side, in the one list call instead."""
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    pillars = []
    for p in db.list_pillars(mission_id):
        payload = _pillar_payload(p)
        payload["open_question_count"] = db.count_open_pillar_questions(p["pillar_id"])
        pillars.append(payload)
    return JSONResponse({"pillars": pillars})


async def get_pillar(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    pillar = db.get_pillar(pillar_id)
    if not pillar:
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    payload = _pillar_payload(pillar)
    if pillar["current_revision_id"]:
        current = db.get_current_pillar_site(pillar_id)
        if current:
            payload["current_revision"] = _revision_payload(current, include_html=False)
    payload["open_question_count"] = db.count_open_pillar_questions(pillar_id)
    last_seen = db.get_pillar_last_seen(pillar_id)
    if last_seen is None:
        payload["since_last_visit"] = {"last_seen_at": None, "revisions": [], "questions": []}
    else:
        payload["since_last_visit"] = {
            "last_seen_at": last_seen,
            "revisions": [
                _revision_payload(r, include_html=False)
                for r in db.list_pillar_site_revisions_since(pillar_id, last_seen)
            ],
            "questions": [
                _question_payload(q)
                for q in db.list_pillar_conversation_since(pillar_id, last_seen)
            ],
        }
    return JSONResponse({"pillar": payload})


async def mark_pillar_seen(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    if not db.get_pillar(pillar_id):
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    seen_at = db.mark_pillar_seen(pillar_id)
    return JSONResponse({"ok": True, "seen_at": seen_at})


async def set_pillar_status(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    body = await request.json()
    status = body.get("status")
    if status not in db.VALID_MISSION_STATUSES:
        return JSONResponse(
            {"error": f"status must be one of {db.VALID_MISSION_STATUSES}"},
            status_code=400,
        )
    ok = db.set_pillar_status(pillar_id, status)
    if not ok:
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    return JSONResponse({"pillar": _pillar_payload(db.get_pillar(pillar_id))})


async def move_question(request: Request) -> JSONResponse:
    """Point a question at a different anchor, or at none.

    Screens get restructured. Without this the only way to keep a
    conversation attached to its subject is to freeze the markup it was asked
    against, which turns last week's question into an argument for keeping
    content nobody needs.
    """
    entry_id = request.path_params["entry_id"]
    body = await request.json()
    anchor = body.get("anchor")
    if anchor is not None and not isinstance(anchor, str):
        return JSONResponse({"error": "anchor must be a string or null"}, status_code=400)
    if not db.set_question_anchor(entry_id, anchor):
        return JSONResponse({"error": "question not found"}, status_code=404)
    return JSONResponse({"question": _question_payload(db.get_conversation_entry(entry_id))})


async def retire_question(request: Request) -> JSONResponse:
    """Retire a question whose subject stopped being relevant.

    Retires, never deletes: what was asked, and why it stopped mattering, is
    part of the record. It leaves the screen and stops counting as open. An
    empty note un-retires it.
    """
    entry_id = request.path_params["entry_id"]
    body = await request.json()
    note = body.get("note", "")
    if not isinstance(note, str):
        return JSONResponse({"error": "note must be a string"}, status_code=400)
    if not db.retire_question(entry_id, note):
        return JSONResponse({"error": "question not found"}, status_code=404)
    return JSONResponse({"question": _question_payload(db.get_conversation_entry(entry_id))})


async def set_pillar_last_done(request: Request) -> JSONResponse:
    """The pillar's status line: the last productive thing that finished.

    Free text on purpose. SKILL.md section 9 sets six rules for writing one
    and they are worth following, but none of them is machine-checkable
    without rejecting good writing a validator failed to parse -- an
    identifier and a proper noun look identical to a regex. The rules are
    for the coordinator to apply; this route only bounds the length.
    """
    pillar_id = request.path_params["pillar_id"]
    body = await request.json()
    text = body.get("last_done")
    if not isinstance(text, str):
        return JSONResponse({"error": "last_done must be a string"}, status_code=400)
    if len(text) > LAST_DONE_MAX:
        return JSONResponse(
            {"error": f"last_done must be at most {LAST_DONE_MAX} characters —"
                      " it is two sentences, not a report"},
            status_code=400,
        )
    if not db.set_pillar_last_done(pillar_id, text):
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    return JSONResponse({"pillar": _pillar_payload(db.get_pillar(pillar_id))})


async def delete_pillar(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    deleted = db.delete_pillar(pillar_id)
    if not deleted:
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def push_pillar_site_revision(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    body = await request.json()
    html = body.get("html")
    if not isinstance(html, str) or not html.strip():
        return JSONResponse({"error": "html is required"}, status_code=400)
    note = (body.get("note") or "").strip()
    revision = db.push_pillar_site_revision(pillar_id, html, note)
    if revision is None:
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    pillar = db.get_pillar(pillar_id)
    _heartbeat_coordinator_presence(
        f"pillar:{pillar_id}", pillar.get("coordinator_session") if pillar else "",
    )
    return JSONResponse(
        {"revision": _revision_payload({**revision, "byte_size": len(html)}, include_html=False)},
        status_code=201,
    )


async def get_current_pillar_site(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    if not db.get_pillar(pillar_id):
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    current = db.get_current_pillar_site(pillar_id)
    if not current:
        return JSONResponse({"error": "pillar has no site revision yet"}, status_code=404)
    return JSONResponse({"revision": _revision_payload(current, include_html=True)})


async def list_pillar_site_revisions(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    if not db.get_pillar(pillar_id):
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    revisions = [
        _revision_payload(r, include_html=False)
        for r in db.list_pillar_site_revisions(pillar_id)
    ]
    return JSONResponse({"revisions": revisions})


async def get_pillar_site_revision(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    revision_id = request.path_params["revision_id"]
    revision = db.get_pillar_site_revision(pillar_id, revision_id)
    if not revision:
        return JSONResponse({"error": "revision not found"}, status_code=404)
    return JSONResponse({"revision": _revision_payload(revision, include_html=True)})


async def activate_pillar_site_revision(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    revision_id = request.path_params["revision_id"]
    if not db.get_pillar(pillar_id):
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    ok = db.activate_pillar_site_revision(pillar_id, revision_id)
    if not ok:
        return JSONResponse({"error": "revision not found"}, status_code=404)
    current = db.get_current_pillar_site(pillar_id)
    return JSONResponse({"revision": _revision_payload(current, include_html=False)})


# ── Cross-pillar decision log ────────────────────────────────────


def _decision_log_payload(entry: dict, pillar_names: dict) -> dict:
    return {
        "log_id": entry["log_id"],
        "mission_id": entry["mission_id"],
        "pillar_id": entry.get("pillar_id"),
        "pillar_name": pillar_names.get(entry.get("pillar_id")),
        "kind": entry["kind"],
        "revision_seq": entry.get("revision_seq"),
        "text": entry["text"],
        "created_at": entry["created_at"],
    }


async def get_decision_log(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    pillar_names = {p["pillar_id"]: p["name"] for p in db.list_pillars(mission_id)}
    entries = [_decision_log_payload(e, pillar_names) for e in db.list_decision_log(mission_id)]
    return JSONResponse({"decision_log": entries})


# ── Public serving: one composed screen (see compose.py) ─────────
#
# No dashboard chrome, stable URL across every future revision push. The
# freshness requirement is explicit (a coordinator watching work-in-progress
# needs the page to reflect a push immediately): every response reads the
# current revision fresh from SQLite and is marked uncacheable end to end,
# so no browser or intermediate proxy can serve a stale copy.

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


def _set_visitor_cookie(response, token: str) -> None:
    response.set_cookie(
        VISITOR_COOKIE, token,
        max_age=VISITOR_COOKIE_MAX_AGE, path="/", httponly=True,
        samesite="lax", secure=True,
    )


async def serve_mission_site(request: Request):
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return PlainTextResponse("Not Found", status_code=404, headers=_NO_STORE_HEADERS)
    # The SAME compose function the relay resolver calls. Two surfaces, one
    # document: a screen served here and a screen served over the channel
    # cannot drift, because there is only one place that builds one.
    document = compose.compose_screen(mission_id)
    if document is None:
        return PlainTextResponse(
            "Mission has no site revision yet", status_code=404, headers=_NO_STORE_HEADERS
        )
    response = HTMLResponse(document, headers=_NO_STORE_HEADERS)
    # First visit carries ?as=<token> in the share link; resolve once and
    # cookie it so every later visit (and every API call the site's own
    # JS makes) is attributed without the token reappearing in the URL.
    as_token = request.query_params.get("as")
    if as_token and db.resolve_visitor(as_token):
        _set_visitor_cookie(response, as_token)
    return response


async def serve_pillar_site(request: Request):
    """Convenience direct-serve URL for one pillar within an already-opened
    mission (bookmarking/refreshing on one pillar) -- never itself the link
    handed out to a guest; the mission-level link is. See SKILL.md's
    pillars section for the one-link-per-mission model."""
    mission_id = request.path_params["mission_id"]
    pillar_id = request.path_params["pillar_id"]
    pillar = db.get_pillar(pillar_id)
    if not pillar or pillar["mission_id"] != mission_id:
        return PlainTextResponse("Not Found", status_code=404, headers=_NO_STORE_HEADERS)
    document = compose.compose_screen(mission_id, pillar_id)
    if document is None:
        return PlainTextResponse(
            "Pillar has no site revision yet", status_code=404, headers=_NO_STORE_HEADERS
        )
    response = HTMLResponse(document, headers=_NO_STORE_HEADERS)
    as_token = request.query_params.get("as")
    if as_token and db.resolve_visitor(as_token):
        _set_visitor_cookie(response, as_token)
    return response


# ── Visitor identity shim (P2) ────────────────────────────────────


async def create_visitor_token(request: Request) -> JSONResponse:
    """API/CLI-only -- the operator mints a token to hand a person a
    share link. No dashboard UI: the operator is a human handing links
    to other humans, but minting is still an API action, same call made
    on missions and site revisions."""
    body = await request.json()
    display_name = (body.get("display_name") or "").strip()
    if not display_name:
        return JSONResponse({"error": "display_name is required"}, status_code=400)
    attachment_id, error = _store_avatar(body.get("avatar"), display_name)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    visitor = db.create_visitor_token(display_name, avatar_attachment_id=attachment_id)
    return JSONResponse({"visitor": _visitor_payload(visitor)}, status_code=201)


#: Inline upload shape for a guest's face. The BYTES do not live here --
#: they go straight into the graph's content-addressed attachment store,
#: which dedups by SHA256, serves same-origin at /api/attachment/<id>, and
#: already has a resumable relay fetch protocol the bootloader speaks.
#: visitor_tokens keeps only the id.
_AVATAR_DATA_RE = re.compile(
    r"^data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=\s]+)$"
)
_AVATAR_MAX_BYTES = 8 * 1024 * 1024


def _store_avatar(value, display_name: str) -> tuple[str | None, str | None]:
    """data: URL -> attachment id. Returns (attachment_id, error).

    Absent is fine and common: a guest without a photo renders the
    initial-and-color avatar the dashboard already derives from their
    participant_id.
    """
    if value is None or value == "":
        return None, None
    if not isinstance(value, str):
        return None, "avatar must be a data:image/... base64 URL"
    match = _AVATAR_DATA_RE.match(value)
    if not match:
        return None, (
            "avatar must be a data:image/<png|jpeg|gif|webp>;base64 URL — the "
            "bytes are stored once in the attachment store, not on the visitor"
        )
    import base64 as _b64
    import binascii
    import tempfile
    from pathlib import Path as _Path

    subtype = match.group(1)
    try:
        raw = _b64.b64decode(match.group(2), validate=False)
    except (binascii.Error, ValueError):
        return None, "avatar base64 did not decode"
    if not raw:
        return None, "avatar is empty"
    if len(raw) > _AVATAR_MAX_BYTES:
        return None, f"avatar exceeds {_AVATAR_MAX_BYTES // (1024 * 1024)}MB"

    ext = "jpg" if subtype in ("jpeg", "jpg") else subtype
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", display_name).strip("-") or "guest"
    with tempfile.TemporaryDirectory() as tmp:
        staged = _Path(tmp) / f"{safe}.{ext}"
        staged.write_bytes(raw)
        from tools.graph import ops as graph_ops
        att = graph_ops.attach_file(
            str(staged),
            # Alt text is not decoration: this face is rendered in the
            # operator's approval dialog and in every viewer, and a
            # participant photo with no textual equivalent is unreadable
            # to anyone using a screen reader.
            alt_text=f"Profile photo of {display_name}",
        )
    return att["id"], None


def _visitor_payload(visitor: dict) -> dict:
    """A visitor as the UI consumes it: the avatar is a URL to the shared
    attachment route, never inline bytes."""
    attachment_id = visitor.get("avatar_attachment_id")
    return {
        **{k: v for k, v in visitor.items() if k != "avatar_attachment_id"},
        "avatar_attachment_id": attachment_id,
        "avatar_url": f"/api/attachment/{attachment_id}" if attachment_id else None,
    }


async def set_visitor_avatar(request: Request) -> JSONResponse:
    """Attach or replace a guest's photo after the fact."""
    participant_id = request.path_params["participant_id"]
    existing = db.get_visitor_by_participant_id(participant_id)
    if not existing:
        return JSONResponse({"error": "participant not found"}, status_code=404)
    body = await request.json()
    attachment_id, error = _store_avatar(
        body.get("avatar"), existing["display_name"],
    )
    if error:
        return JSONResponse({"error": error}, status_code=400)
    visitor = db.set_visitor_avatar(participant_id, attachment_id)
    return JSONResponse({"visitor": _visitor_payload(visitor)})


async def get_visitor_by_participant_id(request: Request) -> JSONResponse:
    """Confirm a participant_id refers to a real, already-minted visitor --
    what personalized mission-grant minting (auto-tp1v9) checks before
    binding a grant to it, so a link never points at a dangling reference.
    Never accepts or returns a token -- participant_id only."""
    participant_id = request.path_params["participant_id"]
    visitor = db.get_visitor_by_participant_id(participant_id)
    if not visitor:
        return JSONResponse({"error": "participant not found"}, status_code=404)
    return JSONResponse({"visitor": visitor})


def _resolve_visitor_identity(request: Request) -> dict | None:
    """Cookie first (the TOKEN, never bare participant_id -- an
    impersonation hole otherwise: participant_id is deliberately
    display-safe and appears in GET .../questions, so authenticating by
    it would let anyone copy an id they saw and post as that person).
    Falls back to ?as=<token> for a same-request ask before the cookie
    has been set."""
    token = request.cookies.get(VISITOR_COOKIE) or request.query_params.get("as")
    if not token:
        return None
    return db.resolve_visitor(token)


# ── Mission conversation (P2 Q&A) ─────────────────────────────────


def _update_payload(update: dict) -> dict:
    return {
        "update_id": update["update_id"],
        "entry_id": update["entry_id"],
        "text": update["text"],
        "created_at": update["created_at"],
    }


def _question_payload(entry: dict) -> dict:
    # Updates are working-in-progress noise, not part of the record: once
    # an entry is answered, its update trail (including anything a reopen
    # folded in -- the prior answer, the follow-up text) is dropped from
    # what guests see. The answer itself is the only thing meant to
    # persist. See reopen_question's docstring for the same call on the
    # write side.
    updates = (
        []
        if entry["answer"] is not None
        else [_update_payload(u) for u in db.list_conversation_updates(entry["entry_id"])]
    )
    return {
        "entry_id": entry["entry_id"],
        "mission_id": entry["mission_id"],
        "pillar_id": entry.get("pillar_id"),
        "anchor": entry.get("anchor"),
        "question": entry["question"],
        "asked_by_participant_id": entry["asked_by_participant_id"],
        "asked_by_label": entry["asked_by_label"],
        "answer": entry["answer"],
        "answered_by_session": entry["answered_by_session"],
        "answered_at": entry["answered_at"],
        "relay_status": entry["relay_status"],
        "created_at": entry["created_at"],
        "retired_at": entry.get("retired_at"),
        "retired_note": entry.get("retired_note"),
        "updates": updates,
    }


async def _relay_question(*, mission_id: str, entry_id: str) -> None:
    """Background delivery, after the visitor's 201 has already gone out
    -- a slow/hung CrossTalk send must never stall their request. Runs
    via Starlette's BackgroundTask (after-response, in-process), not the
    heavier CrosstalkDirective/settings-mediator machinery: that
    substrate is built for operator-UI-triggered, JS-append-driven
    directives, and this is a plain server-side side effect after a
    REST write -- calling tmux_send/build_envelope directly is the
    proportionate amount of machinery.

    Delivery routing: a mission-level message (no pillar_id) goes to the
    mission's own coordinator_session only. A pillar-level message goes to
    BOTH the pillar's coordinator_session (primary responder -- a reply is
    expected) AND the mission's top-level coordinator_session (copied, per
    the operator's standing instruction that Mission Control tracks every
    unit of work as it lands) -- one relay call, two envelopes with
    different instruction text, not two separate code paths.
    """
    from tools.dashboard.surface_actions import build_envelope
    from tools.dashboard.tmux_send import tmux_send

    mission = db.get_mission(mission_id)
    entry = db.get_question(mission_id, entry_id) if mission else None
    if not mission or not entry:
        return

    pillar = db.get_pillar(entry["pillar_id"]) if entry.get("pillar_id") else None
    # Read fresh, not from any value captured at ask time --
    # coordinator_session is mutable by design, on both missions and pillars.
    mission_coordinator = mission.get("coordinator_session") or ""

    if pillar:
        primary_session = pillar.get("coordinator_session") or ""
        primary_label = pillar["name"]
        primary_from_id = f"pillar:{pillar['pillar_id']}"
        reply_route = f"/api/pillars/{pillar['pillar_id']}/questions/{entry_id}"
        cc_session = mission_coordinator
    else:
        primary_session = mission_coordinator
        primary_label = mission["name"]
        primary_from_id = f"mission:{mission_id}"
        reply_route = f"/api/missions/{mission_id}/questions/{entry_id}"
        cc_session = ""

    if not primary_session:
        db.mark_question_relay_status(mission_id, entry_id, "failed")
        logger.warning(
            "mission-question relay skipped for %s/%s: no coordinator_session on the %s",
            mission_id, entry_id, "pillar" if pillar else "mission",
        )
        return

    anchor_note = f" (re: {entry['anchor']})" if entry.get("anchor") else ""
    # Non-empty only after a reopen (see reopen_question) -- a first-round
    # relay always fires before any update could exist. Its presence is
    # what distinguishes "new question" framing from "this was reopened."
    discussion = db.list_conversation_updates(entry_id)
    context_block = ""
    if discussion:
        lines = "\n".join(f"- {u['text']}" for u in discussion)
        context_block = (
            f"\n\nThis question was reopened -- prior context:\n{lines}\n\n"
            f"Write ONE new answer that integrates the whole discussion above, "
            f"not just a reply to the latest line in isolation. Don't reference "
            f"the earlier rounds (\"as I mentioned\", \"following up on\") -- "
            f"the guest only ever sees this one final answer, never the history."
        )
    primary_envelope = build_envelope(
        from_id=primary_from_id,
        kind="mission-question",
        extra={
            "mission": mission_id,
            "pillar": pillar["pillar_id"] if pillar else None,
            "entry_id": entry_id,
        },
        body=(
            f"New message on \"{primary_label}\"{anchor_note} from {entry['asked_by_label']}:\n\n"
            f"{entry['question']}"
            f"{context_block}\n\n"
            f"A reply is expected -- but only once it's actually correct, not "
            f"provisionally. If this will take a while, post interim progress "
            f"visibility any number of times first -- one short, present-tense "
            f"line each (e.g. \"checking the acquisition log\"). Updates are "
            f"ephemeral and disappear once you answer, so don't put anything in "
            f"one that the answer itself needs:\n"
            f"POST {reply_route}/update {{\"text\": \"still working...\"}}\n"
            f"Then file exactly one concise closing answer that stands alone -- "
            f"state the current conclusion and its rationale, not the steps you "
            f"took to get there or references to earlier back-and-forth:\n"
            f"POST {reply_route}/answer {{\"answer\": \"...\"}}\n"
            f"If the guest pushes back later, they may reopen this with a "
            f"follow-up -- you'll get a fresh relay like this one with the "
            f"prior context folded in, and should file a new answer that "
            f"replaces this one entirely."
        ),
    )

    sent_ok = True
    try:
        await tmux_send(primary_session, primary_envelope)
    except Exception:
        sent_ok = False
        logger.warning(
            "mission-question relay failed for %s/%s (primary=%s)",
            mission_id, entry_id, primary_session, exc_info=True,
        )

    if cc_session and cc_session != primary_session:
        cc_envelope = build_envelope(
            from_id=primary_from_id,
            kind="mission-question-cc",
            extra={
                "mission": mission_id,
                "pillar": pillar["pillar_id"] if pillar else None,
                "entry_id": entry_id,
            },
            body=(
                f"Copied for tracking -- no reply expected from you. New message on "
                f"\"{primary_label}\"{anchor_note} from {entry['asked_by_label']}:\n\n"
                f"{entry['question']}"
                f"{context_block}\n\n"
                f"\"{primary_label}\"'s own session ({primary_session}) is expected to reply."
            ),
        )
        try:
            await tmux_send(cc_session, cc_envelope)
        except Exception:
            logger.warning(
                "mission-question cc relay failed for %s/%s (cc=%s)",
                mission_id, entry_id, cc_session, exc_info=True,
            )

    db.mark_question_relay_status(mission_id, entry_id, "sent" if sent_ok else "failed")


async def handle_relay_write(participant_id: str, mission_id: str, body: dict) -> dict | None:
    """The relay's ``write`` op (`tools/dashboard/link_serving.py`) for a
    ``mission``-type grant, registered into that module's target_type
    dispatch. The relay hands us only ``(identity, mission_id, body)`` --
    identity already resolved from the grant, never read out of `body` --
    and forwards `body` completely uninterpreted; everything about what a
    mission write means lives here, not in the relay.

    Returns the response envelope's payload on success, or None to have
    the relay refuse this body on its own terms (bad request).
    """
    visitor = db.get_visitor_by_participant_id(participant_id)
    if not visitor:
        return None  # the bound identity no longer resolves
    participant_label = visitor["display_name"]

    # Every guest today is named and pre-minted (Milestone 1) -- this is
    # not an extension point yet. When Milestone 2's anonymous, self-serve
    # guests exist, a pre-screening check (e.g. prompt-injection
    # detection) on `question`/`followup` belongs right here, before the
    # text reaches ask_question/reopen_question and from there a
    # coordinator's tmux session.

    kind = body.get("kind")
    if kind == "question":
        question = body.get("question")
        if not isinstance(question, str) or not question.strip():
            return None
        anchor = body.get("anchor")
        if anchor is not None and not isinstance(anchor, str):
            return None
        anchor = (anchor or "").strip() or None
        pillar_id = body.get("pillar_id")
        if pillar_id is not None:
            if not isinstance(pillar_id, str):
                return None
            # A guest's channel is bound to ONE mission (mission_id, from
            # the grant) -- a pillar_id belonging to a DIFFERENT mission
            # must be refused here, not left to ask_question's own
            # existence check (which only confirms the pillar exists at
            # all, not that it's this mission's). Otherwise this channel
            # could relay straight to an unrelated mission's pillar
            # coordinator.
            pillar = db.get_pillar(pillar_id)
            if not pillar or pillar["mission_id"] != mission_id:
                return None
        entry = db.ask_question(
            mission_id, question, participant_id, participant_label,
            pillar_id=pillar_id, anchor=anchor,
        )
        event = "asked"
    elif kind == "reopen":
        entry_id = body.get("entry_id")
        followup = body.get("followup")
        if not isinstance(entry_id, str) or not entry_id:
            return None
        if not isinstance(followup, str) or not followup.strip():
            return None
        entry = db.reopen_question(mission_id, entry_id, followup, participant_label)
        event = "reopened"
    else:
        return None

    if entry is None:
        return None  # mission/entry not found, or reopen on an unanswered entry

    entry_payload = _question_payload(entry)
    await _publish_conversation_event(
        event, mission_id, entry.get("pillar_id"), entry["entry_id"], question=entry_payload,
    )
    # Fire-and-forget, same contract as the HTTP path's BackgroundTask: a
    # slow/hung CrossTalk send must never stall the guest's response.
    asyncio.create_task(
        _relay_question(mission_id=mission_id, entry_id=entry["entry_id"])
    )
    return {"question": entry_payload}


def _pillar_of_this_mission(pillar_id, mission_id: str) -> dict | None:
    """A pillar_id from a guest's request body is only usable if it
    belongs to THIS channel's own mission. Same check, same reason, as
    handle_relay_write's -- a guest's channel is bound to one mission and
    must never reach another mission's content through a supplied id."""
    if not isinstance(pillar_id, str) or not pillar_id:
        return None
    pillar = db.get_pillar(pillar_id)
    if not pillar or pillar["mission_id"] != mission_id:
        return None
    return pillar


def _mission_presence(mission_id: str) -> list[dict]:
    """Presence rows for this mission's own surface.

    Rows are keyed ``{surface_id}:{participant_id}`` and a mission's
    surface_id is itself ``mission:<mission_id>``, so the prefix match
    below is three-part by construction -- the same shape the relay
    publisher's presence routing depends on.
    """
    from tools.graph import settings_ops
    from tools.graph.surface import SURFACE_PRESENCE_SET_ID

    prefix = f"mission:{mission_id}:"
    try:
        rows = settings_ops.read_set(
            SURFACE_PRESENCE_SET_ID, org=compose.PRESENCE_ORG,
        )
    except (LookupError, OSError, ValueError):
        return []  # store genuinely unavailable; a missing arg is not that
    out = []
    for member in rows.members:
        if not isinstance(member.key, str) or not member.key.startswith(prefix):
            continue
        payload = member.payload if isinstance(member.payload, dict) else {}
        out.append({
            "participant_id": payload.get("participant_id"),
            "participant_label": payload.get("participant_label"),
            "participant_kind": payload.get("participant_kind"),
            "state": payload.get("state"),
            "heartbeat_at": payload.get("heartbeat_at"),
        })
    return out


async def handle_relay_read(participant_id: str, mission_id: str, body: dict) -> dict | None:
    """The relay's ``read`` op for a ``mission`` grant (auto-t2lz1).

    The read mirror of :func:`handle_relay_write`: the relay hands over
    ``(identity, mission_id, body)`` and never interprets ``body``, so
    everything a mission read MEANS lives here. This is what the mission
    site's own ``fetch('/api/...')`` calls become over the relay, where
    the dashboard's origin does not exist.

    Returns the response payload, or None for the relay to refuse.
    """
    if not db.get_mission(mission_id):
        return None
    kind = body.get("kind")

    if kind == "pillars":
        return {"pillars": [_pillar_payload(p) for p in db.list_pillars(mission_id)]}

    if kind == "questions":
        pillar_id = body.get("pillar_id")
        if pillar_id is None:
            entries = db.list_conversation(mission_id)
        else:
            if _pillar_of_this_mission(pillar_id, mission_id) is None:
                return None
            entries = db.list_pillar_conversation(pillar_id)
        return {"questions": [_question_payload(e) for e in entries]}

    if kind == "presence":
        return {"presence": _mission_presence(mission_id)}

    if kind == "here":
        # A reader saying "I am looking at this". Presence otherwise only
        # records sessions that PUSH -- so the people a mission is written for
        # never appeared on it, and a coordinator reading for an hour without
        # pushing looked absent.
        #
        # An unbound link has no participant, and everyone holding it is the
        # same participant as far as anything here can tell. It says so
        # plainly rather than inventing distinct visitors.
        pillar_id = body.get("pillar_id")
        if pillar_id is not None and _pillar_of_this_mission(pillar_id, mission_id) is None:
            return None
        surface_id = f"pillar:{pillar_id}" if pillar_id else f"mission:{mission_id}"
        who = participant_id or "guest:with-the-link"
        label = participant_id or "Someone with the link"
        _heartbeat_presence(surface_id, who, label, kind="person")
        return {"presence": _surface_presence(surface_id)}

    if kind == "mission_site":
        # The way back. Navigation could reach every pillar and never the
        # screen it started on, because only pillars had a read.
        document = compose.compose_screen(mission_id)
        if document is None:
            return None
        return {"document": document.decode("utf-8")}

    if kind == "pillar_site":
        # A composed screen, not raw author HTML: the viewer document.writes
        # what it receives, so anything short of a complete document would
        # land without the runtime that made the navigation possible.
        pillar_id = body.get("pillar_id")
        if _pillar_of_this_mission(pillar_id, mission_id) is None:
            return None
        document = compose.compose_screen(mission_id, pillar_id)
        if document is None:
            return None
        return {"pillar_id": pillar_id, "document": document.decode("utf-8")}

    return None


async def _ask_question_impl(
    request: Request, *, mission_id: str, pillar_id: str | None,
) -> JSONResponse:
    body = await request.json()
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        return JSONResponse({"error": "question is required"}, status_code=400)
    anchor = (body.get("anchor") or "").strip() or None

    visitor = _resolve_visitor_identity(request)
    if not visitor:
        return JSONResponse(
            {"error": "visitor identity required — missing or unresolved token"},
            status_code=401,
        )

    entry = db.ask_question(
        mission_id, question, visitor["participant_id"], visitor["participant_label"],
        pillar_id=pillar_id, anchor=anchor,
    )
    if entry is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    entry_payload = _question_payload(entry)
    await _publish_conversation_event(
        "asked", mission_id, pillar_id, entry["entry_id"], question=entry_payload,
    )
    response = JSONResponse(
        {"question": entry_payload},
        status_code=201,
        background=BackgroundTask(_relay_question, mission_id=mission_id, entry_id=entry["entry_id"]),
    )
    as_token = request.query_params.get("as")
    if as_token and not request.cookies.get(VISITOR_COOKIE):
        _set_visitor_cookie(response, as_token)
    return response


async def ask_question(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    return await _ask_question_impl(request, mission_id=mission_id, pillar_id=None)


async def ask_pillar_question(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    pillar = db.get_pillar(pillar_id)
    if not pillar:
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    return await _ask_question_impl(request, mission_id=pillar["mission_id"], pillar_id=pillar_id)


async def list_conversation(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    entries = [_question_payload(e) for e in db.list_conversation(mission_id)]
    return JSONResponse({"questions": entries})


async def list_pillar_conversation(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    if not db.get_pillar(pillar_id):
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    entries = [_question_payload(e) for e in db.list_pillar_conversation(pillar_id)]
    return JSONResponse({"questions": entries})


async def _answer_question_impl(
    request: Request, *, mission_id: str, pillar_id: str | None,
    entry_id: str, responder_session: str,
) -> JSONResponse:
    body = await request.json()
    answer = body.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return JSONResponse({"error": "answer is required"}, status_code=400)
    entry = db.answer_question(mission_id, entry_id, answer, responder_session)
    if entry is None:
        return JSONResponse({"error": "question not found"}, status_code=404)
    surface_id = f"pillar:{pillar_id}" if pillar_id else f"mission:{mission_id}"
    _heartbeat_coordinator_presence(surface_id, responder_session)
    entry_payload = _question_payload(entry)
    await _publish_conversation_event(
        "answered", mission_id, pillar_id, entry_id, question=entry_payload,
    )
    return JSONResponse({"question": entry_payload})


async def answer_question(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    entry_id = request.path_params["entry_id"]
    mission = db.get_mission(mission_id)
    if not mission:
        return JSONResponse({"error": "mission not found"}, status_code=404)
    # Snapshot who actually answered, read fresh right now -- not a value
    # captured back at ask time, since coordinator_session is mutable.
    answered_by_session = mission.get("coordinator_session") or ""
    return await _answer_question_impl(
        request, mission_id=mission_id, pillar_id=None,
        entry_id=entry_id, responder_session=answered_by_session,
    )


async def answer_pillar_question(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    entry_id = request.path_params["entry_id"]
    pillar = db.get_pillar(pillar_id)
    if not pillar:
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    answered_by_session = pillar.get("coordinator_session") or ""
    return await _answer_question_impl(
        request, mission_id=pillar["mission_id"], pillar_id=pillar_id,
        entry_id=entry_id, responder_session=answered_by_session,
    )


async def _reopen_question_impl(
    request: Request, *, mission_id: str, pillar_id: str | None, entry_id: str,
) -> JSONResponse:
    """Guest-side follow-up on an already-answered entry -- pushback after
    not liking the answer, or a natural next round of the same discussion.
    Reuses the entry_id (see reopen_question's docstring): the record ends
    up as one integrated question/answer pair, not a growing thread.
    """
    body = await request.json()
    followup = body.get("followup")
    if not isinstance(followup, str) or not followup.strip():
        return JSONResponse({"error": "followup is required"}, status_code=400)

    visitor = _resolve_visitor_identity(request)
    if not visitor:
        return JSONResponse(
            {"error": "visitor identity required — missing or unresolved token"},
            status_code=401,
        )

    entry = db.reopen_question(mission_id, entry_id, followup, visitor["participant_label"])
    if entry is None:
        return JSONResponse(
            {"error": "question not found, or not yet answered"}, status_code=404,
        )

    entry_payload = _question_payload(entry)
    await _publish_conversation_event(
        "reopened", mission_id, pillar_id, entry_id, question=entry_payload,
    )
    return JSONResponse(
        {"question": entry_payload},
        background=BackgroundTask(_relay_question, mission_id=mission_id, entry_id=entry_id),
    )


async def reopen_question(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    entry_id = request.path_params["entry_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    return await _reopen_question_impl(
        request, mission_id=mission_id, pillar_id=None, entry_id=entry_id,
    )


async def reopen_pillar_question(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    entry_id = request.path_params["entry_id"]
    pillar = db.get_pillar(pillar_id)
    if not pillar:
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    return await _reopen_question_impl(
        request, mission_id=pillar["mission_id"], pillar_id=pillar_id, entry_id=entry_id,
    )


async def _add_update_impl(
    request: Request, *, lookup_mission_id: str, entry_id: str,
) -> JSONResponse:
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    existing = db.get_question(lookup_mission_id, entry_id)
    if not existing:
        return JSONResponse({"error": "question not found"}, status_code=404)
    update = db.add_conversation_update(entry_id, text)
    update_payload = _update_payload(update)
    await _publish_conversation_event(
        "update", lookup_mission_id, existing.get("pillar_id"), entry_id,
        update=update_payload,
    )
    return JSONResponse({"update": update_payload}, status_code=201)


async def add_question_update(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    entry_id = request.path_params["entry_id"]
    return await _add_update_impl(request, lookup_mission_id=mission_id, entry_id=entry_id)


async def add_pillar_question_update(request: Request) -> JSONResponse:
    pillar_id = request.path_params["pillar_id"]
    entry_id = request.path_params["entry_id"]
    pillar = db.get_pillar(pillar_id)
    if not pillar:
        return JSONResponse({"error": "pillar not found"}, status_code=404)
    return await _add_update_impl(request, lookup_mission_id=pillar["mission_id"], entry_id=entry_id)


routes: list[Route] = [
    Route("/api/missions", list_missions, methods=["GET"]),
    Route("/api/missions", create_mission, methods=["POST"]),
    Route("/api/missions/{mission_id}", get_mission, methods=["GET"]),
    Route("/api/missions/{mission_id}", delete_mission, methods=["DELETE"]),
    Route("/api/missions/{mission_id}/seen", mark_mission_seen, methods=["POST"]),
    Route("/api/missions/{mission_id}/status", set_mission_status, methods=["POST"]),
    Route("/api/missions/{mission_id}/site", push_site_revision, methods=["POST"]),
    Route("/api/missions/{mission_id}/site", get_current_site, methods=["GET"]),
    Route(
        "/api/missions/{mission_id}/site/revisions",
        list_site_revisions, methods=["GET"],
    ),
    Route(
        "/api/missions/{mission_id}/site/revisions/{revision_id}",
        get_site_revision, methods=["GET"],
    ),
    Route(
        "/api/missions/{mission_id}/site/revisions/{revision_id}/activate",
        activate_site_revision, methods=["POST"],
    ),
    Route("/api/visitor-tokens", create_visitor_token, methods=["POST"]),
    Route(
        "/api/visitor-tokens/{participant_id}",
        get_visitor_by_participant_id, methods=["GET"],
    ),
    Route(
        "/api/visitor-tokens/{participant_id}/avatar",
        set_visitor_avatar, methods=["POST"],
    ),
    Route("/api/missions/{mission_id}/questions", ask_question, methods=["POST"]),
    Route("/api/missions/{mission_id}/questions", list_conversation, methods=["GET"]),
    Route(
        "/api/missions/{mission_id}/questions/{entry_id}/answer",
        answer_question, methods=["POST"],
    ),
    Route(
        "/api/missions/{mission_id}/questions/{entry_id}/update",
        add_question_update, methods=["POST"],
    ),
    Route(
        "/api/missions/{mission_id}/questions/{entry_id}/reopen",
        reopen_question, methods=["POST"],
    ),
    Route("/api/missions/{mission_id}/decision-log", get_decision_log, methods=["GET"]),
    Route("/missions/{mission_id}", serve_mission_site, methods=["GET"]),

    # ── Pillars ──────────────────────────────────────────────────
    Route("/api/missions/{mission_id}/pillars", create_pillar, methods=["POST"]),
    Route("/api/missions/{mission_id}/pillars", list_pillars, methods=["GET"]),
    Route("/api/pillars/{pillar_id}", get_pillar, methods=["GET"]),
    Route("/api/pillars/{pillar_id}", delete_pillar, methods=["DELETE"]),
    Route("/api/pillars/{pillar_id}/seen", mark_pillar_seen, methods=["POST"]),
    Route("/api/pillars/{pillar_id}/status", set_pillar_status, methods=["POST"]),
    Route("/api/pillars/{pillar_id}/last-done", set_pillar_last_done, methods=["POST"]),
    Route("/api/questions/{entry_id}/anchor", move_question, methods=["POST"]),
    Route("/api/questions/{entry_id}/retire", retire_question, methods=["POST"]),
    Route("/api/pillars/{pillar_id}/site", push_pillar_site_revision, methods=["POST"]),
    Route("/api/pillars/{pillar_id}/site", get_current_pillar_site, methods=["GET"]),
    Route(
        "/api/pillars/{pillar_id}/site/revisions",
        list_pillar_site_revisions, methods=["GET"],
    ),
    Route(
        "/api/pillars/{pillar_id}/site/revisions/{revision_id}",
        get_pillar_site_revision, methods=["GET"],
    ),
    Route(
        "/api/pillars/{pillar_id}/site/revisions/{revision_id}/activate",
        activate_pillar_site_revision, methods=["POST"],
    ),
    Route("/api/pillars/{pillar_id}/questions", ask_pillar_question, methods=["POST"]),
    Route("/api/pillars/{pillar_id}/questions", list_pillar_conversation, methods=["GET"]),
    Route(
        "/api/pillars/{pillar_id}/questions/{entry_id}/answer",
        answer_pillar_question, methods=["POST"],
    ),
    Route(
        "/api/pillars/{pillar_id}/questions/{entry_id}/update",
        add_pillar_question_update, methods=["POST"],
    ),
    Route(
        "/api/pillars/{pillar_id}/questions/{entry_id}/reopen",
        reopen_pillar_question, methods=["POST"],
    ),
    Route("/missions/{mission_id}/pillars/{pillar_id}", serve_pillar_site, methods=["GET"]),
]
