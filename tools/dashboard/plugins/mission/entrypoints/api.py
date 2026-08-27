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
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from starlette.routing import Route

from tools.dashboard.api_auth import (
    ApiPrincipalKind,
    organization_scope_from_request,
    principal_from_request,
)
from tools.dashboard.plugins.mission import compose, writes
from tools.dashboard.plugins.mission.entrypoints.schemas import (
    MISSION_SET_ID,
    PILLAR_SET_ID,
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


def _operator_persona(org: str | None) -> str | None:
    """This dashboard's member persona in *org* — one settings call.

    ``autonomy.network.persona`` is written at the found/join ceremony,
    keyed by the persona public key: the stable member id the signed-
    settings envelope will carry as ``terminal_persona``. Storing THIS
    (never a resolved label) is the final form — display name and icon
    resolve from the member directory at render time.
    """
    if not org:
        return None
    try:
        from tools.graph import ops as graph_ops
        rows = list(graph_ops.read_set(
            "autonomy.network.persona", org=org, peers=[]))
        if len(rows) == 1:
            return rows[0].key
        for m in rows:
            if (m.payload or {}).get("source") in ("found", "join"):
                return m.key
    except Exception:
        pass
    return None


def _identity(request: Request, org: str | None = None) -> str:
    """Attribution identity from the API boundary, never the body.

    An org session: its readable session name. The operator: their org
    member persona public key — the same identity the signed-settings
    envelope will stamp as terminal_persona, resolved to their chosen
    per-org display name and icon at render. "operator" only when the
    org has no persona ceremony recorded.
    """
    principal = principal_from_request(request)
    if principal.kind is ApiPrincipalKind.OPERATOR_COOKIE             or (principal.global_authority and not principal.subject):
        return _operator_persona(org) or "operator"
    if principal.subject:
        return principal.subject
    return "unknown"


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
            row = {"mission_id": m.key, "org": org, **dict(m.payload)}
            try:
                row["activity"] = compose.activity_summary(org, m.key)
            except Exception:
                row["activity"] = None
            rows.append(row)
    # Lifecycle groups first (active, paused, complete), recency within
    # each — the many-missions ordering the homepage renders directly.
    order = {"active": 0, "paused": 1, "complete": 2}
    rows.sort(key=lambda r: (
        order.get(r.get("status") or "active", 0),
        -(((r.get("activity") or {}).get("last_at")) or 0),
        r.get("name") or ""))
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


async def mission_screen(request: Request):
    """The complete mission document. ``?pillar=<id>`` opens focused.

    ``?progress=1`` streams stage markers ahead of the document so the
    loading interstitial can narrate the compose (settings counts, bead
    bridge) on the same round trip — no separate progress endpoint.
    """
    mission_id = request.path_params["mission_id"]
    org = _owning_org(request, mission_id)
    focus = request.query_params.get("pillar")
    if org and request.query_params.get("progress"):
        if compose.load_mission(org, mission_id) is None:
            return JSONResponse({"error": "unknown mission"},
                                status_code=404)
        # identity encoding is load-bearing: GZipMiddleware skips
        # responses that already declare one. Under gzip, zlib sits on
        # the tiny stage markers until the deflate buffer fills — the
        # browser would receive the whole narration at once, defeating
        # the streaming interstitial (curl streamed; browsers did not).
        return StreamingResponse(
            compose.render_stages(org, mission_id, focus),
            media_type="text/html",
            headers={"Content-Encoding": "identity"})
    doc = compose.render_screen(org, mission_id, focus) if org else None
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
            by=_identity(request, org),
            at=(body or {}).get("at"),
            turn=(body or {}).get("turn"))
    except Exception as exc:                      # noqa: BLE001
        return _refused(exc)
    return JSONResponse({"ok": True, "state": item["state"]})


async def _relay_to_coordinator(org: str | None, mission_id: str,
                                pillar_id: str, by: str,
                                note: str) -> bool:
    """Best-effort CrossTalk to the pillar's coordinator (storage always
    precedes delivery; a failed relay never loses the write)."""
    try:
        pillars = compose.load_pillars(org, mission_id)
        me = next((x for x in pillars
                   if x["pillar_id"] == pillar_id), None)
        target = (me or {}).get("coordinator_session")
        if not target or target == by:
            return False
        from tools.dashboard.crosstalk_delivery import deliver_from_chat
        out = await deliver_from_chat(by, target, note)
        return bool(out.get("delivered"))
    except Exception:                             # noqa: BLE001
        return False


def _entry_route(fn, what: str = "entry", **fixed):
    async def handler(request: Request) -> JSONResponse:
        pp = request.path_params
        org = _owning_org(request, pp["mission_id"])
        text = await _text_body(request)
        if text is None:
            return JSONResponse({"error": "text required"}, status_code=400)
        by = _identity(request, org)
        try:
            fn(org, pp["mission_id"], pp["pillar_id"], pp["item_id"],
               text=text, by=by, **fixed)
        except Exception as exc:                  # noqa: BLE001
            return _refused(exc)
        # The chat route always relayed; item entries silently did not —
        # a question reply reached the record but never the coordinator
        # (found live by the operator on the first cross-org mission).
        relayed = await _relay_to_coordinator(
            org, pp["mission_id"], pp["pillar_id"], by,
            f"[mission {what} \u00b7 {pp['pillar_id']}] "
            f"on item {pp['item_id']}: {text}\n"
            f"View: /mission/{pp['mission_id']}")
        return JSONResponse({"ok": True, "relayed": relayed})
    return handler


async def post_chat(request: Request) -> JSONResponse:
    """One message into the pillar's untracked log."""
    pp = request.path_params
    org = _owning_org(request, pp["mission_id"])
    text = await _text_body(request)
    if text is None:
        return JSONResponse({"error": "text required"}, status_code=400)
    by = _identity(request, org)
    try:
        entries = writes.add_chat(org, pp["mission_id"], pp["pillar_id"],
                                  text=text, by=by)
    except Exception as exc:                      # noqa: BLE001
        return _refused(exc)
    # Storage precedes delivery, and delivery is best-effort: the pillar's
    # coordinator hears about the message over CrossTalk when one is
    # declared and live; a failed relay never loses the message.
    relayed = await _relay_to_coordinator(
        org, pp["mission_id"], pp["pillar_id"], by,
        f"[mission chat \u00b7 {pp['pillar_id']}] {text}\n"
        f"Reply with: graph mission chat {pp['mission_id']} "
        f"{pp['pillar_id']} \"...\"")
    return JSONResponse({"ok": True, "entries": entries, "relayed": relayed})


async def allocation(request: Request) -> JSONResponse:
    """Sessions by pillar across every visible mission — who holds what.

    Joins each pillar's coordinator_session with the dashboard's live
    session records (label + liveness), so the screen can show the
    pillar's name beside the session's own title.
    """
    labels: dict[str, dict] = {}
    try:
        from tools.dashboard.dao import dashboard_db
        for row in dashboard_db.get_all_sessions():
            name = row.get("tmux_name") or ""
            if name:
                labels[name] = {"label": row.get("label") or "",
                                "live": bool(row.get("is_live"))}
        live = {r.get("tmux_name") for r in dashboard_db.get_live_sessions()}
        for name in labels:
            labels[name]["live"] = name in live
    except Exception:
        pass
    out = []
    from tools.graph import ops as graph_ops
    for org in _org_scopes(request):
        try:
            members = graph_ops.read_set(MISSION_SET_ID, org=org, peers=[])
        except Exception:
            continue
        for m in members:
            mission = {"mission_id": m.key, "org": org,
                       "name": (m.payload or {}).get("name") or "",
                       "status": (m.payload or {}).get("status") or "active",
                       "pillars": []}
            for pl in compose.load_pillars(org, m.key):
                sid = pl.get("coordinator_session") or ""
                info = labels.get(sid) or {}
                mission["pillars"].append({
                    "pillar_id": pl["pillar_id"],
                    "name": pl.get("name") or "",
                    "color": pl.get("color") or "",
                    "session": sid,
                    "session_title": info.get("label") or "",
                    "live": bool(info.get("live")),
                })
            out.append(mission)
    out.sort(key=lambda r: r.get("name") or "")
    return JSONResponse({"missions": out})


#: The session-viewer cross-link glyph (same concentric mark the
#: legacy plugin used, so the operator's muscle memory carries over).
_SESSION_ICON = (
    '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    '<circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.5"/>'
    '<circle cx="8" cy="8" r="2.4" fill="currentColor"/></svg>'
)


def session_contributions(session_ids: list[str],
                          request: Request) -> dict[str, list[dict]]:
    """Session-viewer chrome: coordinated pillars link into /mission.

    Reverse lookup over the mission.* settings (pillar payloads carry
    ``coordinator_session``), scoped exactly like every other read here.
    Completed missions contribute nothing — a live session should not
    badge into a retired record.
    """
    from tools.graph import ops as graph_ops
    result: dict[str, list[dict]] = {sid: [] for sid in session_ids}
    wanted = set(session_ids)
    for org in _org_scopes(request):
        try:
            missions = {m.key: dict(m.payload) for m in
                        graph_ops.read_set(MISSION_SET_ID, org=org,
                                           peers=[])}
            pillars = graph_ops.read_set(PILLAR_SET_ID, org=org, peers=[])
        except Exception:
            continue
        badged: set[tuple[str, str]] = set()   # (session, mission_id)
        for m in pillars:
            payload = dict(m.payload or {})
            coord = str(payload.get("coordinator_session") or "")
            if coord not in wanted:
                continue
            mission_id, _, pillar_id = m.key.partition(":")
            mission = missions.get(mission_id)
            if not mission or (mission.get("status") or "active") == \
                    "complete":
                continue
            mission_name = str(mission.get("name") or "Mission Control")
            pillar_name = str(payload.get("name") or "pillar")
            badged.add((coord, mission_id))
            result[coord].append({
                "id": f"mission-pillar:{m.key}",
                "kind": "action",
                "label": mission_name,
                "title": f"Open {mission_name} \u2014 {pillar_name} "
                         "in Mission Control",
                # the mission app always lands whole; ?pillar= focuses
                "href": f"/mission/{mission_id}",
                "icon_svg": _SESSION_ICON,
                "accent": str(payload.get("color") or "#8b85ff"),
                "hard_reload": False,
            })
        # The MISSION coordinator badges too: the registry row's own
        # coordinator_session is what mission-surface chat/question
        # relays target, so after the allocation-truth fix (pillars
        # naming the sessions actually driving them, mission-level
        # coordination on the registry) it showed no icon while pillar
        # coordinators did. Deduped: a session already badged for this
        # mission via a pillar keeps its pillar-accented entry.
        for mission_id, mission in missions.items():
            coord = str((mission or {}).get("coordinator_session") or "")
            if (coord not in wanted
                    or (mission.get("status") or "active") == "complete"
                    or (coord, mission_id) in badged):
                continue
            mission_name = str(mission.get("name") or "Mission Control")
            result[coord].append({
                "id": f"mission-coordinator:{mission_id}",
                "kind": "action",
                "label": mission_name,
                "title": f"Open {mission_name} \u2014 mission "
                         "coordinator in Mission Control",
                "href": f"/mission/{mission_id}",
                "icon_svg": _SESSION_ICON,
                "accent": "#8b85ff",
                "hard_reload": False,
            })
    return result


async def post_mission_status(request: Request) -> JSONResponse:
    """Transition a mission's lifecycle: {status: active|paused|complete}."""
    mission_id = request.path_params["mission_id"]
    org = _owning_org(request, mission_id)
    if org is None:
        return JSONResponse({"error": "unknown mission"}, status_code=404)
    try:
        body = await request.json()
        status = (body or {}).get("status")
        mission = compose.load_mission(org, mission_id) or {}
        mission["status"] = status
        mission["status_changed_at"] = writes.now_iso()
        from tools.graph import settings_ops
        from tools.dashboard.plugins.mission.entrypoints.schemas import (
            SCHEMA_REVISION,
        )
        settings_ops.upsert_by_key(MISSION_SET_ID, SCHEMA_REVISION,
                                   mission_id, mission, org=org)
    except Exception as exc:                      # noqa: BLE001
        return _refused(exc)
    return JSONResponse({"ok": True, "status": status})


_ITEM = "/api/mission/item/{mission_id}/{pillar_id}/{item_id}"

routes: list = [
    Route("/api/mission/missions", list_missions, methods=["GET"]),
    Route("/api/mission/screen/{mission_id}", mission_screen, methods=["GET"]),
    Route("/api/mission/status/{mission_id}", post_mission_status,
          methods=["POST"]),
    Route("/api/mission/allocation", allocation, methods=["GET"]),
    Route("/api/mission/pillars/{mission_id}", list_pillars, methods=["GET"]),
    Route("/api/mission/items/{mission_id}", list_items, methods=["GET"]),
    Route("/api/mission/tasks/{mission_id}", list_tasks, methods=["GET"]),
    Route("/api/mission/chat/{mission_id}/{pillar_id}", get_chat,
          methods=["GET"]),
    Route(_ITEM, put_item, methods=["PUT"]),
    Route(_ITEM + "/state", post_state, methods=["POST"]),
    Route(_ITEM + "/work", _entry_route(writes.add_work, what="work note"),
          methods=["POST"]),
    Route(_ITEM + "/reply",
          _entry_route(writes.add_discussion, what="question reply"),
          methods=["POST"]),
    Route(_ITEM + "/progress",
          _entry_route(writes.add_discussion, what="progress update",
                       progress=True),
          methods=["POST"]),
    Route(_ITEM + "/answer",
          _entry_route(writes.answer_question, what="answer"),
          methods=["POST"]),
    Route("/api/mission/chat/{mission_id}/{pillar_id}", post_chat,
          methods=["POST"]),
]
