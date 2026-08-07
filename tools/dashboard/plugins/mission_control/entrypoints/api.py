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

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from tools.dashboard.dao import mission_control_db as db


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
        {"revision": _revision_payload({**revision, "html": html}, include_html=False)},
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


async def serve_mission_site(request: Request):
    mission_id = request.path_params["mission_id"]
    if not db.get_mission(mission_id):
        return PlainTextResponse("Not Found", status_code=404, headers=_NO_STORE_HEADERS)
    current = db.get_current_site(mission_id)
    if not current:
        return PlainTextResponse(
            "Mission has no site revision yet", status_code=404, headers=_NO_STORE_HEADERS
        )
    return HTMLResponse(current["html"], headers=_NO_STORE_HEADERS)


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
    Route("/missions/{mission_id}", serve_mission_site, methods=["GET"]),
]
