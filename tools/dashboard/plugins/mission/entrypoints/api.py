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
    ApiPrincipalKind,
    organization_scope_from_request,
    principal_from_request,
)
from tools.dashboard.plugins.mission import compose, writes
from tools.dashboard.plugins.mission.entrypoints.schemas import (
    MISSION_SET_ID,
)
from tools.graph.schemas.registry import SchemaValidationError


def _org_scopes(request: Request) -> list[str]:
    """The org databases this caller's reads may span.

    An org-bound session reads its own org. A dashboard operator or
    local host session with no explicit org selection holds global
    authority — for them the honest answer is every org, aggregated
    (the same shape the legacy mission list serves), not the personal
    database that a literal None scope would resolve to.
    """
    org = organization_scope_from_request(request)
    if org:
        return [org]
    if principal_from_request(request).global_authority:
        try:
            from tools.graph.cross_org import list_org_slugs
            return [o for o in list_org_slugs() if o != "personal"]
        except Exception:
            return []
    return []


def _identity(request: Request) -> str:
    """Attribution label from the API boundary, never the body.

    An org session's subject is its readable session name (auto-...);
    an operator cookie's subject is an opaque cookie-session hex that
    means nothing on a screen — the operator's messages say "operator".
    """
    principal = principal_from_request(request)
    if principal.kind is ApiPrincipalKind.OPERATOR_COOKIE:
        return "operator"
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
    """Every mission visible to the caller: org-bound sessions see their
    org; the operator sees all orgs aggregated."""
    from tools.graph import ops as graph_ops
    rows = []
    for org in _org_scopes(request):
        try:
            members = graph_ops.read_set(MISSION_SET_ID, org=org, peers=[])
        except Exception:
            continue
        for m in members:
            rows.append({"mission_id": m.key, "org": org, **dict(m.payload)})
    rows.sort(key=lambda r: r.get("name") or "")
    return JSONResponse({"missions": rows})


def _owning_org(request: Request, mission_id: str) -> str | None:
    for org in _org_scopes(request):
        try:
            if compose.load_mission(org, mission_id) is not None:
                return org
        except Exception:
            continue
    return None


async def list_pillars(request: Request) -> JSONResponse:
    """The mission's pillars in declared order: ``{pillars: [...]}``."""
    org = _owning_org(request, request.path_params["mission_id"])
    return JSONResponse({"pillars": compose.load_pillars(
        org, request.path_params["mission_id"]) if org else []})


async def list_items(request: Request) -> JSONResponse:
    """Viewer-shaped items; ``?pillar=<id>`` narrows to one surface."""
    org = _owning_org(request, request.path_params["mission_id"])
    items = compose.load_items(org, request.path_params["mission_id"]) \
        if org else []
    pillar = request.query_params.get("pillar")
    if pillar:
        items = [i for i in items if i["surface_id"] == pillar]
    return JSONResponse({"items": items})


async def list_tasks(request: Request) -> JSONResponse:
    """The bead-bridge payload: ``{tasks: {pillar_id: [...]}}``."""
    mission_id = request.path_params["mission_id"]
    org = _owning_org(request, mission_id)
    pillars = compose.load_pillars(org, mission_id) if org else []
    return JSONResponse(
        {"tasks": compose.load_beads(org, mission_id, pillars)})


async def get_chat(request: Request) -> JSONResponse:
    """One pillar's chat log: ``{entries: [...]}``."""
    pp = request.path_params
    org = _owning_org(request, pp["mission_id"])
    logs = compose.load_chat(org, pp["mission_id"]) if org else {}
    return JSONResponse({"entries": logs.get(pp["pillar_id"], [])})


async def mission_screen(request: Request) -> HTMLResponse | JSONResponse:
    """The complete mission document. ``?pillar=<id>`` opens focused."""
    mission_id = request.path_params["mission_id"]
    org = _owning_org(request, mission_id)
    doc = compose.render_screen(
        org, mission_id, request.query_params.get("pillar")) \
        if org else None
    if doc is None:
        return JSONResponse({"error": "unknown mission"}, status_code=404)
    return HTMLResponse(doc)


async def put_item(request: Request) -> JSONResponse:
    """Create or fully rewrite one item; the schema is the gate."""
    pp = request.path_params
    org = _owning_org(request, pp["mission_id"]) \
        or organization_scope_from_request(request)
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
    pp = request.path_params
    org = _owning_org(request, pp["mission_id"])
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
        pp = request.path_params
        org = _owning_org(request, pp["mission_id"])
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
    pp = request.path_params
    org = _owning_org(request, pp["mission_id"])
    text = await _text_body(request)
    if text is None:
        return JSONResponse({"error": "text required"}, status_code=400)
    by = _identity(request)
    try:
        entries = writes.add_chat(org, pp["mission_id"], pp["pillar_id"],
                                  text=text, by=by)
    except Exception as exc:                      # noqa: BLE001
        return _refused(exc)
    # Storage precedes delivery, and delivery is best-effort: the pillar's
    # coordinator hears about the message over CrossTalk when one is
    # declared and live; a failed relay never loses the message.
    relayed = False
    try:
        pillars = compose.load_pillars(org, pp["mission_id"])
        me = next((x for x in pillars
                   if x["pillar_id"] == pp["pillar_id"]), None)
        target = (me or {}).get("coordinator_session")
        if target and target != by:
            from tools.dashboard.crosstalk_delivery import deliver_from_chat
            note = (f"[mission chat \u00b7 {me.get('name') or pp['pillar_id']}] "
                    f"{text}\n"
                    f"Reply with: graph mission chat {pp['mission_id']} "
                    f"{pp['pillar_id']} \"...\"")
            out = await deliver_from_chat(by, target, note)
            relayed = bool(out.get("delivered"))
    except Exception:                             # noqa: BLE001
        pass
    return JSONResponse({"ok": True, "entries": entries, "relayed": relayed})


_ITEM = "/api/mission/item/{mission_id}/{pillar_id}/{item_id}"

routes: list = [
    Route("/api/mission/missions", list_missions, methods=["GET"]),
    Route("/api/mission/screen/{mission_id}", mission_screen, methods=["GET"]),
    Route("/api/mission/pillars/{mission_id}", list_pillars, methods=["GET"]),
    Route("/api/mission/items/{mission_id}", list_items, methods=["GET"]),
    Route("/api/mission/tasks/{mission_id}", list_tasks, methods=["GET"]),
    Route("/api/mission/chat/{mission_id}/{pillar_id}", get_chat,
          methods=["GET"]),
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
