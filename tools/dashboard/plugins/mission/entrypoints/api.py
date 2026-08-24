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

from tools.dashboard.api_auth import (
    organization_scope_from_request,
    principal_from_request,
)
from tools.dashboard.plugins.mission import compose, writes
from tools.dashboard.plugins.mission.entrypoints.schemas import (
    MISSION_SET_ID,
)
from tools.graph.schemas.registry import SchemaValidationError


def _identity(request: Request) -> str:
    """Attribution label from the API boundary, never the body."""
    principal = principal_from_request(request)
    if principal.subject:
        return principal.subject
    return "operator" if principal.global_authority else "unknown"


async def _text_body(request: Request) -> str | None:
    try:
        body = await request.json()
    except Exception:
        return None
    text = (body or {}).get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip()


def _refused(exc: Exception) -> JSONResponse:
    status = 400 if isinstance(exc, (writes.WriteRefused,
                                     SchemaValidationError)) else 500
    return JSONResponse({"error": str(exc)}, status_code=status)


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


async def put_item(request: Request) -> JSONResponse:
    """Create or fully rewrite one item; the schema is the gate."""
    org = organization_scope_from_request(request)
    pp = request.path_params
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise writes.WriteRefused("payload must be an object")
        writes.upsert_item(org, pp["mission_id"], pp["pillar_id"],
                           pp["item_id"], payload)
    except Exception as exc:                      # noqa: BLE001
        return _refused(exc)
    return JSONResponse({"ok": True})


async def post_state(request: Request) -> JSONResponse:
    """Checkpoint transition: history appended, confirmation stamped."""
    org = organization_scope_from_request(request)
    pp = request.path_params
    try:
        body = await request.json()
        item = writes.transition(
            org, pp["mission_id"], pp["pillar_id"], pp["item_id"],
            to_state=str((body or {}).get("state") or ""),
            by=_identity(request),
            at=(body or {}).get("at"),
            turn=(body or {}).get("turn"))
    except Exception as exc:                      # noqa: BLE001
        return _refused(exc)
    return JSONResponse({"ok": True, "state": item["state"]})


def _entry_route(fn, **fixed):
    async def handler(request: Request) -> JSONResponse:
        org = organization_scope_from_request(request)
        pp = request.path_params
        text = await _text_body(request)
        if text is None:
            return JSONResponse({"error": "text required"}, status_code=400)
        try:
            fn(org, pp["mission_id"], pp["pillar_id"], pp["item_id"],
               text=text, by=_identity(request), **fixed)
        except Exception as exc:                  # noqa: BLE001
            return _refused(exc)
        return JSONResponse({"ok": True})
    return handler


async def post_chat(request: Request) -> JSONResponse:
    """One message into the pillar's untracked log."""
    org = organization_scope_from_request(request)
    pp = request.path_params
    text = await _text_body(request)
    if text is None:
        return JSONResponse({"error": "text required"}, status_code=400)
    try:
        entries = writes.add_chat(org, pp["mission_id"], pp["pillar_id"],
                                  text=text, by=_identity(request))
    except Exception as exc:                      # noqa: BLE001
        return _refused(exc)
    return JSONResponse({"ok": True, "entries": entries})


_ITEM = "/api/mission/item/{mission_id}/{pillar_id}/{item_id}"

routes: list = [
    Route("/api/mission/missions", list_missions, methods=["GET"]),
    Route("/api/mission/screen/{mission_id}", mission_screen, methods=["GET"]),
    Route(_ITEM, put_item, methods=["PUT"]),
    Route(_ITEM + "/state", post_state, methods=["POST"]),
    Route(_ITEM + "/work", _entry_route(writes.add_work), methods=["POST"]),
    Route(_ITEM + "/reply", _entry_route(writes.add_discussion),
          methods=["POST"]),
    Route(_ITEM + "/progress",
          _entry_route(writes.add_discussion, progress=True),
          methods=["POST"]),
    Route(_ITEM + "/answer", _entry_route(writes.answer_question),
          methods=["POST"]),
    Route("/api/mission/chat/{mission_id}/{pillar_id}", post_chat,
          methods=["POST"]),
]
