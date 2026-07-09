"""Jira broker routes — the ``issue_tracker`` capability's dashboard half.

Reads are direct: an agent's ``jira-read``/``jira-createmeta`` shim calls these
routes and the Jira call runs here, host-side — the container never holds the
token. Writes never execute from an agent call at all: the agent stages a
``kind=jira_write`` approval request, the operator approves or declines in the
overlay, and the ``jira_write`` executor registered below performs the call as
a post-approval backend task, delivering the outcome through the approval
result. Ops carried in the request JSON:

- ``{"op": "comment", "key", "body_markdown"}``
- ``{"op": "set_field", "key", "field_name" | "field_id", "body_markdown"}``
  (e.g. Confirm Plan; the id is discovered via editmeta at execution time)
- ``{"op": "create", "fields": {...}}``
- ``{"op": "attach", "key", "filename", "content_b64", "mime_type"?}``
"""

from __future__ import annotations

import asyncio
import base64

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agents.capabilities.jira.backend import api, queries
from tools.dashboard import approvals_routes


def _cfg(org: str | None) -> api.JiraConfig:
    return api.JiraConfig.resolve(org=org)


def _org(request: Request) -> str | None:
    """Org whose install Setting configures the broker — from ?org= (the
    agent tools pass their container's GRAPH_ORG), same pattern as
    /api/sign-key."""
    return request.query_params.get("org") or None


async def get_issue(request: Request) -> JSONResponse:
    """GET /api/jira/issue/{key} -> cleaned ticket, ADF already markdown."""
    key = request.path_params["key"]
    try:
        ticket = await asyncio.to_thread(api.read_ticket, _cfg(_org(request)), key)
    except api.JiraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(ticket)


async def get_createmeta(request: Request) -> JSONResponse:
    """GET /api/jira/createmeta?project=&issuetype=&version_prefix="""
    project = request.query_params.get("project", "")
    issuetype = request.query_params.get("issuetype", "")
    version_prefix = request.query_params.get("version_prefix") or None
    if not project or not issuetype:
        return JSONResponse({"error": "project and issuetype are required"},
                            status_code=400)
    try:
        meta = await asyncio.to_thread(
            api.createmeta, _cfg(_org(request)), project, issuetype, version_prefix)
    except api.JiraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(meta)


async def get_attachment(request: Request) -> Response:
    """GET /api/jira/attachment/{id} -> the attachment bytes (download runs
    host-side; the signed media redirect never reaches the agent)."""
    attachment_id = request.path_params["id"]
    try:
        content, filename, mime_type = await asyncio.to_thread(
            api.get_attachment, _cfg(_org(request)), attachment_id)
    except api.JiraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return Response(content, media_type=mime_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})


async def get_probe(request: Request) -> JSONResponse:
    """GET /api/jira/probe -> config + auth reachability for the capability."""
    try:
        result = await asyncio.to_thread(api.probe, _cfg(_org(request)))
    except api.JiraError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse(result)


async def post_search(request: Request) -> JSONResponse:
    """POST /api/jira/search?org= — body ``{jql, max_results?, page_token?}``.

    A read route like ``/api/jira/issue``: the JQL runs host-side with the
    broker credential, no operator approval. Search grants nothing
    ``jira-read`` doesn't already grant — it only removes the need to
    guess issue keys."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "request body must be a JSON object"},
                            status_code=400)
    jql = str(body.get("jql") or "").strip()
    if not jql:
        return JSONResponse({"error": "jql is required"}, status_code=400)
    try:
        out = await asyncio.to_thread(
            api.search_issues, _cfg(_org(request)), jql,
            int(body.get("max_results") or 50),
            body.get("page_token") or None)
    except api.JiraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(out)


def _workspace_overrides(session: str) -> tuple[str, dict]:
    """Resolve the calling session's workspace and return
    ``(workspace_id, issue_tracker workspace_overrides)``.

    Resolution is host-side and keyed by the session name the trusted
    launcher stamped into the dashboard DB — a session cannot wander into
    another workspace's query set by naming a different workspace. Raises
    ``LookupError`` when the session doesn't map to a workspace."""
    from agents.workspace_settings import (
        WORKSPACE_CAPABILITY_ENABLE_SET_ID, get_workspace,
    )
    from tools.dashboard.dao import dashboard_db
    from tools.graph import ops as graph_ops

    row = dashboard_db.get_session(session)
    project = ((row or {}).get("project") or "").strip()
    if not project:
        raise LookupError(
            f"session {session!r} does not map to a workspace")
    try:
        ws = get_workspace(project)
    except KeyError as e:
        raise LookupError(str(e)) from e
    members = graph_ops.read_set(
        WORKSPACE_CAPABILITY_ENABLE_SET_ID, org=ws.graph_project, peers=[])
    for m in members.members:
        if m.key == f"{ws.id}:issue_tracker":
            overrides = m.payload.get("workspace_overrides")
            return ws.id, overrides if isinstance(overrides, dict) else {}
    return ws.id, {}


# Query-string keys the named-query route consumes itself; everything else
# is a query parameter (name=value) for placeholder substitution.
_RESERVED_QUERY_PARAMS = {"org", "session", "max_results", "page_token"}


async def list_named_queries(request: Request) -> JSONResponse:
    """GET /api/jira/query?session= — the calling workspace's named
    queries, shaped for discovery (``jira-query --list``)."""
    session = request.query_params.get("session") or ""
    if not session:
        return JSONResponse({"error": "session is required"}, status_code=400)
    try:
        workspace_id, overrides = await asyncio.to_thread(
            _workspace_overrides, session)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse({"workspace": workspace_id,
                         "queries": queries.list_queries(overrides)})


async def run_named_query(request: Request) -> JSONResponse:
    """GET /api/jira/query/{name}?session=&param=value… — resolve a named
    query from the calling workspace's enable Setting and run it."""
    name = request.path_params["name"]
    session = request.query_params.get("session") or ""
    if not session:
        return JSONResponse({"error": "session is required"}, status_code=400)
    params = {k: v for k, v in request.query_params.items()
              if k not in _RESERVED_QUERY_PARAMS}
    try:
        workspace_id, overrides = await asyncio.to_thread(
            _workspace_overrides, session)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    try:
        jql = queries.resolve_query(overrides, name, params)
    except queries.QueryError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        out = await asyncio.to_thread(
            api.search_issues, _cfg(_org(request)), jql,
            int(request.query_params.get("max_results") or 50),
            request.query_params.get("page_token") or None)
    except api.JiraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"workspace": workspace_id, "query": name,
                         "jql": jql, **out})


async def _execute_jira_write(row: dict) -> dict:
    """Post-approval executor for ``kind=jira_write`` (see approvals_routes:
    runs as a backend task after the operator's verdict; its return value is
    stored as ``result.execution`` and wakes the agent's held GET)."""
    req = row["request"]
    op = req.get("op")
    cfg = _cfg(req.get("org") or None)

    def run() -> dict:
        if op == "comment":
            out = api.add_comment(cfg, req["key"], req.get("body_markdown", ""))
        elif op == "set_field":
            field_id = req.get("field_id") or api.editmeta_field_id(
                cfg, req["key"], req.get("field_name", ""))
            out = api.set_field(cfg, req["key"], field_id,
                                req.get("body_markdown", ""))
        elif op == "create":
            out = api.create_issue(cfg, req.get("fields", {}))
        elif op == "attach":
            out = api.add_attachment(
                cfg, req["key"], req.get("filename", "attachment"),
                base64.b64decode(req.get("content_b64", "")),
                req.get("mime_type") or "application/octet-stream")
        else:
            return {"ok": False, "error": f"unknown jira_write op: {op}"}
        return {"ok": True, **out}

    return await asyncio.to_thread(run)


approvals_routes.EXECUTORS["jira_write"] = _execute_jira_write


ROUTES = [
    Route("/api/jira/issue/{key}", get_issue, methods=["GET"]),
    Route("/api/jira/createmeta", get_createmeta, methods=["GET"]),
    Route("/api/jira/attachment/{id}", get_attachment, methods=["GET"]),
    Route("/api/jira/search", post_search, methods=["POST"]),
    Route("/api/jira/query", list_named_queries, methods=["GET"]),
    Route("/api/jira/query/{name}", run_named_query, methods=["GET"]),
    Route("/api/jira/probe", get_probe, methods=["GET"]),
]
