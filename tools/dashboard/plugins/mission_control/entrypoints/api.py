"""Mission Control backend API.

P1: missions + native chromeless site hosting. Entity model is deliberately
minimal — a mission is ``{mission_id, name, coordinator_session,
created_at, current_revision_id, status}``. Resources and live data feeds
arrive with their own phases and are not guessed at here.

Storage is Mission Control's own (``tools.dashboard.dao.mission_control_db``),
not a foreign key into Design Studio's design/revision tables — see that
module's docstring for why. A push to a mission's site both stores AND
publishes in one call; there is no separate "mark shown" step.
"""
from __future__ import annotations

import logging

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from tools.dashboard.dao import mission_control_db as db

logger = logging.getLogger(__name__)

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
            org="autonomy",
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


# ── Chromeless public serving ────────────────────────────────────
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
    current = db.get_current_site(mission_id)
    if not current:
        return PlainTextResponse(
            "Mission has no site revision yet", status_code=404, headers=_NO_STORE_HEADERS
        )
    response = HTMLResponse(current["html"], headers=_NO_STORE_HEADERS)
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
    current = db.get_current_pillar_site(pillar_id)
    if not current:
        return PlainTextResponse(
            "Pillar has no site revision yet", status_code=404, headers=_NO_STORE_HEADERS
        )
    response = HTMLResponse(current["html"], headers=_NO_STORE_HEADERS)
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
    visitor = db.create_visitor_token(display_name)
    return JSONResponse({"visitor": visitor}, status_code=201)


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
        "updates": [_update_payload(u) for u in db.list_conversation_updates(entry["entry_id"])],
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
            f"{entry['question']}\n\n"
            f"A reply is expected -- but only once it's actually correct, not "
            f"provisionally. If this will take a while, post interim progress "
            f"visibility any number of times first:\n"
            f"POST {reply_route}/update {{\"text\": \"still working...\"}}\n"
            f"Then file exactly one concise closing answer:\n"
            f"POST {reply_route}/answer {{\"answer\": \"...\"}}"
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
                f"{entry['question']}\n\n"
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

    response = JSONResponse(
        {"question": _question_payload(entry)},
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
    return JSONResponse({"question": _question_payload(entry)})


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


async def _add_update_impl(
    request: Request, *, lookup_mission_id: str, entry_id: str,
) -> JSONResponse:
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    if not db.get_question(lookup_mission_id, entry_id):
        return JSONResponse({"error": "question not found"}, status_code=404)
    update = db.add_conversation_update(entry_id, text)
    return JSONResponse({"update": _update_payload(update)}, status_code=201)


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
    Route("/api/missions/{mission_id}/decision-log", get_decision_log, methods=["GET"]),
    Route("/missions/{mission_id}", serve_mission_site, methods=["GET"]),

    # ── Pillars ──────────────────────────────────────────────────
    Route("/api/missions/{mission_id}/pillars", create_pillar, methods=["POST"]),
    Route("/api/missions/{mission_id}/pillars", list_pillars, methods=["GET"]),
    Route("/api/pillars/{pillar_id}", get_pillar, methods=["GET"]),
    Route("/api/pillars/{pillar_id}", delete_pillar, methods=["DELETE"]),
    Route("/api/pillars/{pillar_id}/seen", mark_pillar_seen, methods=["POST"]),
    Route("/api/pillars/{pillar_id}/status", set_pillar_status, methods=["POST"]),
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
    Route("/missions/{mission_id}/pillars/{pillar_id}", serve_pillar_site, methods=["GET"]),
]
