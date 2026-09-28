"""Jira broker routes — the ``issue_tracker`` capability's dashboard half.

Reads are direct: an agent's ``jira-read``/``jira-createmeta`` shim calls these
routes and the Jira call runs here, host-side — the container never holds the
token. Writes never execute from an agent call at all: the agent opens a
``kind=jira_write`` Central approval, the operator decides it in the Central
inbox, and jira_central.py performs the write once on this machine, delivering
the outcome through the approval result. Ops carried in the request JSON:

- ``{"op": "comment", "key", "body_markdown"}``
- ``{"op": "set_field", "key", "field_name" | "field_id", "body_markdown"}``
  (the id and schema are discovered via editmeta at execution time; rich text
  becomes ADF and structured values are coerced to Jira's object/array shape)
- ``{"op": "create", "fields": {...}}``
- ``{"op": "attach", "key", "filename", "content_b64", "mime_type", "size"?}``
- ``{"op": "transition", "key", "transition", "fields"?}`` (fields keyed by
  display name or id, values as CLI strings — coerced per schema host-side)
- ``{"op": "change_type", "key", "issue_type"}`` (Jira's "Move": target name
  resolved to the project-scoped id host-side; sub-task conversions rejected)
- ``{"op": "set_story_points", "key", "value", "board_id", "field_id",
  "previous_value"?}`` (uses Jira
  Software's estimation endpoint so the field need not be on the edit screen)
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agents.capabilities.jira.backend import api, queries
from tools.dashboard import api_auth


def _cfg(org: str | None) -> api.JiraConfig:
    return api.JiraConfig.resolve(org=org)


def _org(request: Request) -> str | None:
    """Org whose install Setting configures the broker — the caller's TRUSTED
    org, and nothing the request body can name.

    Bound by ApiIdentityMiddleware (``organization_scope_from_request``): for an
    org session it is the bearer's org, authoritative and un-widenable; for a
    global operator it is their explicit selection. This route deliberately does
    NOT read a ``?org=`` query parameter — a client-supplied org is exactly the
    spoofable, drift-prone input the org-scope standardization removed
    everywhere else. The container tools stopped carrying ``GRAPH_ORG`` to pass
    once the org became a property of the token, so there is nothing legitimate
    to read from the query anyway.
    """
    return api_auth.organization_scope_from_request(request)


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


async def get_fields(request: Request) -> JSONResponse:
    """GET /api/jira/fields/{key} -> editable field metadata.

    This is intentionally a direct read route: agents can discover exact
    display names, ids, schema types, and allowed values before staging a
    write for operator approval.
    """
    key = request.path_params["key"]
    try:
        fields = await asyncio.to_thread(
            api.list_editable_fields, _cfg(_org(request)), key)
    except api.JiraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"fields": fields})


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


async def get_transitions(request: Request) -> JSONResponse:
    """GET /api/jira/transitions/{key} -> the workflow transitions valid
    from the issue's current status, each with its required screen fields
    annotated ``has_value``. The jira-transition tool preflights against
    this so a doomed transition fails with a clear missing-fields list
    BEFORE the operator sees an approval overlay."""
    key = request.path_params["key"]
    try:
        out = await asyncio.to_thread(
            api.list_transitions, _cfg(_org(request)), key)
    except api.JiraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"transitions": out})


async def get_issue_types(request: Request) -> JSONResponse:
    """GET /api/jira/issue-types/{key} -> the issue types valid in the
    ticket's project + its current type. The jira-change-type preflight
    reads this so an invalid target fails with the valid list BEFORE any
    approval overlay opens; the overlay reads it for trusted context."""
    key = request.path_params["key"]
    try:
        out = await asyncio.to_thread(
            api.list_issue_types, _cfg(_org(request)), key)
    except api.JiraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(out)


async def get_estimation(request: Request) -> JSONResponse:
    """GET /api/jira/estimation/{key} -> board, field, and current value."""
    key = request.path_params["key"]
    board_id = request.query_params.get("board_id") or None
    try:
        out = await asyncio.to_thread(
            api.estimation_context, _cfg(_org(request)), key, board_id)
    except (api.JiraError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(out)


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


class _JiraAuthError(Exception):
    """Refusal with an HTTP status. Everything in the auth ladder fails
    closed."""

    def __init__(self, message: str, *, status: int):
        super().__init__(message)
        self.status = status


def _authorized_workspace(authorization: str | None) -> tuple[str, dict]:
    """Authenticate the caller from the bearer token and resolve their
    workspace's ``issue_tracker`` query overrides. Blocking (DB + graph
    reads); call via :func:`asyncio.to_thread`.

    The caller asserts no session and no workspace — both derive host-side
    from launcher-stamped state — so there is nothing to forge. A token
    whose session doesn't map to a workspace, or whose workspace does not
    enable issue_tracker, receives 403."""
    from tools.dashboard.capability_gate import CapabilityRefused, require_capability

    try:
        scope = require_capability(authorization, "issue_tracker")
        return _workspace_overrides(scope.session)
    except CapabilityRefused as e:
        raise _JiraAuthError(e.detail, status=e.status) from e
    except LookupError as e:
        raise _JiraAuthError(str(e), status=403) from e


# Query-string keys the named-query route consumes itself; everything else
# is a query parameter (name=value) for placeholder substitution. ``session``
# is no longer read for identity (it comes from the bearer token) but stays
# reserved so a stray ``?session=`` never becomes a JQL placeholder value.
_RESERVED_QUERY_PARAMS = {"org", "session", "max_results", "page_token"}


async def list_named_queries(request: Request) -> JSONResponse:
    """GET /api/jira/query — the calling workspace's named queries, shaped
    for discovery (``jira-query --list``). Identity is the bearer token."""
    try:
        workspace_id, overrides = await asyncio.to_thread(
            _authorized_workspace, request.headers.get("Authorization"))
    except _JiraAuthError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return JSONResponse({"workspace": workspace_id,
                         "queries": queries.list_queries(overrides)})


async def run_named_query(request: Request) -> JSONResponse:
    """GET /api/jira/query/{name}?param=value… — resolve a named query from
    the calling workspace's enable Setting and run it. Identity is the
    bearer token; the workspace is never caller-asserted."""
    name = request.path_params["name"]
    params = {k: v for k, v in request.query_params.items()
              if k not in _RESERVED_QUERY_PARAMS}
    try:
        workspace_id, overrides = await asyncio.to_thread(
            _authorized_workspace, request.headers.get("Authorization"))
    except _JiraAuthError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
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


ROUTES = [
    Route("/api/jira/issue/{key}", get_issue, methods=["GET"]),
    Route("/api/jira/createmeta", get_createmeta, methods=["GET"]),
    Route("/api/jira/fields/{key}", get_fields, methods=["GET"]),
    Route("/api/jira/attachment/{id}", get_attachment, methods=["GET"]),
    Route("/api/jira/transitions/{key}", get_transitions, methods=["GET"]),
    Route("/api/jira/issue-types/{key}", get_issue_types, methods=["GET"]),
    Route("/api/jira/estimation/{key}", get_estimation, methods=["GET"]),
    Route("/api/jira/search", post_search, methods=["POST"]),
    Route("/api/jira/query", list_named_queries, methods=["GET"]),
    Route("/api/jira/query/{name}", run_named_query, methods=["GET"]),
    Route("/api/jira/probe", get_probe, methods=["GET"]),
]
