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

from agents.capabilities.jira.backend import api
from tools.dashboard import approvals_routes


def _cfg() -> api.JiraConfig:
    return api.JiraConfig.resolve()


async def get_issue(request: Request) -> JSONResponse:
    """GET /api/jira/issue/{key} -> cleaned ticket, ADF already markdown."""
    key = request.path_params["key"]
    try:
        ticket = await asyncio.to_thread(api.read_ticket, _cfg(), key)
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
            api.createmeta, _cfg(), project, issuetype, version_prefix)
    except api.JiraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(meta)


async def get_attachment(request: Request) -> Response:
    """GET /api/jira/attachment/{id} -> the attachment bytes (download runs
    host-side; the signed media redirect never reaches the agent)."""
    attachment_id = request.path_params["id"]
    try:
        content, filename, mime_type = await asyncio.to_thread(
            api.get_attachment, _cfg(), attachment_id)
    except api.JiraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return Response(content, media_type=mime_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})


async def get_probe(request: Request) -> JSONResponse:
    """GET /api/jira/probe -> config + auth reachability for the capability."""
    try:
        result = await asyncio.to_thread(api.probe, _cfg())
    except api.JiraError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse(result)


async def _execute_jira_write(row: dict) -> dict:
    """Post-approval executor for ``kind=jira_write`` (see approvals_routes:
    runs as a backend task after the operator's verdict; its return value is
    stored as ``result.execution`` and wakes the agent's held GET)."""
    req = row["request"]
    op = req.get("op")
    cfg = _cfg()

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
    Route("/api/jira/probe", get_probe, methods=["GET"]),
]
