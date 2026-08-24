"""HTTP surface of the ``mission`` plugin.

Screens are complete documents rendered from the plugin's own settings
sets with the task payload baked in; all interactivity is plain
same-origin fetch against these routes. The plugin serves trusted
templates, so there is no chrome-mediated control mounting, and (v1,
by decision) no relay serving.

Authentication is the substrate's: plugin routes are wrapped in
default-deny auth by the route builder; identity middleware supplies
the trusted organization scope — no org ever arrives in a path or body.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from tools.dashboard.api_auth import organization_scope_from_request
from tools.dashboard.plugins.mission import compose
from tools.dashboard.plugins.mission.entrypoints.schemas import (
    MISSION_SET_ID,
)


async def list_missions(request: Request) -> JSONResponse:
    """Every mission in the caller's org: ``{missions: [{mission_id, ...}]}``."""
    org = organization_scope_from_request(request)
    from tools.graph import ops as graph_ops
    rows = []
    for m in graph_ops.read_set(MISSION_SET_ID, org=org or None, peers=[]):
        rows.append({"mission_id": m.key, **dict(m.payload)})
    rows.sort(key=lambda r: r.get("name") or "")
    return JSONResponse({"missions": rows})


async def mission_screen(request: Request) -> HTMLResponse | JSONResponse:
    """The complete mission document. ``?pillar=<id>`` opens focused."""
    org = organization_scope_from_request(request)
    mission_id = request.path_params["mission_id"]
    doc = compose.render_screen(
        org, mission_id, request.query_params.get("pillar"))
    if doc is None:
        return JSONResponse({"error": "unknown mission"}, status_code=404)
    return HTMLResponse(doc)


routes: list = [
    Route("/api/mission/missions", list_missions, methods=["GET"]),
    Route("/api/mission/screen/{mission_id}", mission_screen, methods=["GET"]),
]
