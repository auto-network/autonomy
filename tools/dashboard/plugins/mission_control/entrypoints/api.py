"""Mission Control backend API.

P1: missions + native chromeless site hosting. Entity model is deliberately
minimal — a mission is ``{mission_id, name, coordinator_session,
created_at}``. Resources, Q&A, and live data feeds arrive with their own
phases (P2/P3) and are not guessed at here.

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
    }


def _revision_payload(revision: dict, *, include_html: bool) -> dict:
    payload = {
        "revision_id": revision["revision_id"],
        "mission_id": revision["mission_id"],
        "revision_seq": revision["revision_seq"],
        "note": revision["note"],
        "created_at": revision["created_at"],
    }
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
    return JSONResponse({"mission": payload})


async def delete_mission(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    deleted = db.delete_mission(mission_id)
    if not deleted:
        return JSONResponse({"error": "mission not found"}, status_code=404)
    return JSONResponse({"ok": True})


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


def _question_payload(entry: dict) -> dict:
    return {
        "entry_id": entry["entry_id"],
        "mission_id": entry["mission_id"],
        "question": entry["question"],
        "asked_by_participant_id": entry["asked_by_participant_id"],
        "asked_by_label": entry["asked_by_label"],
        "answer": entry["answer"],
        "answered_by_session": entry["answered_by_session"],
        "answered_at": entry["answered_at"],
        "relay_status": entry["relay_status"],
        "created_at": entry["created_at"],
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
    """
    from tools.dashboard.surface_actions import build_envelope
    from tools.dashboard.tmux_send import tmux_send

    mission = db.get_mission(mission_id)
    entry = db.get_question(mission_id, entry_id) if mission else None
    if not mission or not entry:
        return
    # Read fresh, not from any value captured at ask time --
    # coordinator_session is mutable by design.
    coordinator_session = mission.get("coordinator_session") or ""
    if not coordinator_session:
        db.mark_question_relay_status(mission_id, entry_id, "failed")
        logger.warning(
            "mission-question relay skipped for %s/%s: mission has no coordinator_session",
            mission_id, entry_id,
        )
        return
    envelope = build_envelope(
        from_id=f"mission:{mission_id}",
        kind="mission-question",
        extra={
            "mission": mission_id,
            "mission_name": mission["name"],
            "entry_id": entry_id,
        },
        body=(
            f"New question on \"{mission['name']}\" from {entry['asked_by_label']}:\n\n"
            f"{entry['question']}\n\n"
            f"Answer: POST /api/missions/{mission_id}/questions/{entry_id}/answer "
            '{"answer": "..."}'
        ),
    )
    try:
        await tmux_send(coordinator_session, envelope)
        db.mark_question_relay_status(mission_id, entry_id, "sent")
    except Exception:
        db.mark_question_relay_status(mission_id, entry_id, "failed")
        logger.warning(
            "mission-question relay failed for %s/%s", mission_id, entry_id, exc_info=True,
        )


async def ask_question(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    body = await request.json()
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        return JSONResponse({"error": "question is required"}, status_code=400)

    visitor = _resolve_visitor_identity(request)
    if not visitor:
        return JSONResponse(
            {"error": "visitor identity required — missing or unresolved token"},
            status_code=401,
        )

    entry = db.ask_question(
        mission_id, question, visitor["participant_id"], visitor["participant_label"],
    )
    if entry is None:
        return JSONResponse({"error": "mission not found"}, status_code=404)

    response = JSONResponse(
        {"question": _question_payload(entry)},
        status_code=201,
        background=BackgroundTask(_relay_question, mission_id=mission_id, entry_id=entry["entry_id"]),
    )
    as_token = request.query_params.get("as")
    if as_token and not request.cookies.get(VISITOR_COOKIE):
        _set_visitor_cookie(response, as_token)
    return response


async def list_conversation(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return JSONResponse({"error": "mission not found"}, status_code=404)
    entries = [_question_payload(e) for e in db.list_conversation(mission_id)]
    return JSONResponse({"questions": entries})


async def answer_question(request: Request) -> JSONResponse:
    mission_id = request.path_params["mission_id"]
    entry_id = request.path_params["entry_id"]
    mission = db.get_mission(mission_id)
    if not mission:
        return JSONResponse({"error": "mission not found"}, status_code=404)
    body = await request.json()
    answer = body.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return JSONResponse({"error": "answer is required"}, status_code=400)

    # Snapshot who actually answered, read fresh right now -- not a value
    # captured back at ask time, since coordinator_session is mutable.
    answered_by_session = mission.get("coordinator_session") or ""
    entry = db.answer_question(mission_id, entry_id, answer, answered_by_session)
    if entry is None:
        return JSONResponse({"error": "question not found"}, status_code=404)
    return JSONResponse({"question": _question_payload(entry)})


routes: list[Route] = [
    Route("/api/missions", list_missions, methods=["GET"]),
    Route("/api/missions", create_mission, methods=["POST"]),
    Route("/api/missions/{mission_id}", get_mission, methods=["GET"]),
    Route("/api/missions/{mission_id}", delete_mission, methods=["DELETE"]),
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
    Route("/missions/{mission_id}", serve_mission_site, methods=["GET"]),
]
