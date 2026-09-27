"""Mailbox capability broker: mediated read-only mail, sending after approval.

The ``mail-*`` tools in a session (agents/capabilities/mailbox/tools) call
these routes with the session's bearer. The mailbox password never leaves
the dashboard process: agents/capabilities/mailbox/backend/api.py reads it
from the audited vault and talks IMAP/SMTP host-side.

Every route resolves the calling session from its bearer (never from a
caller-supplied name) and refuses unless that session's workspace has the
``mailbox`` capability enabled (``autonomy.workspace.capability.enable#1``
key ``<workspace>:mailbox``, enabled not false). The org whose install
Setting and vault slot are used is the bearer's org.

Reads run directly and read-only. Sending is an ``email_send`` approval: the
tool stages the exact message, the operator sees it in the approval overlay,
and ``_execute_email_send`` sends it only after an approval.
"""

from __future__ import annotations

import asyncio
import hashlib

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.capabilities.mailbox.backend import api
from tools.dashboard import approvals_routes

CONTRACT = "mailbox"


class _Refusal(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _caller(request: Request) -> tuple[str, str | None]:
    """(session, org) of the bearer; 401 when missing, unknown or revoked."""
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer ") or not auth[7:]:
        raise _Refusal("missing bearer token: send Authorization: Bearer $CROSSTALK_TOKEN", 401)
    from tools.dashboard.dao import auth_db
    resolved = auth_db.resolve_token(hashlib.sha256(auth[7:].encode()).hexdigest())
    if resolved is None:
        raise _Refusal("invalid or revoked token", 401)
    return resolved[0], resolved[1]


def _require_enabled(session: str) -> str:
    """The session's workspace id, if it has the mailbox capability enabled."""
    from agents.workspace_settings import WORKSPACE_CAPABILITY_ENABLE_SET_ID, get_workspace
    from tools.dashboard.dao import dashboard_db
    from tools.graph import ops as graph_ops

    row = dashboard_db.get_session(session)
    project = ((row or {}).get("project") or "").strip()
    if not project:
        raise _Refusal(f"session {session!r} does not map to a workspace", 403)
    try:
        ws = get_workspace(project)
    except KeyError as e:
        raise _Refusal(str(e), 403) from None
    members = graph_ops.read_set(WORKSPACE_CAPABILITY_ENABLE_SET_ID, org=ws.graph_project, peers=[])
    for m in members.members:
        if m.key == f"{ws.id}:{CONTRACT}":
            payload = m.payload if isinstance(m.payload, dict) else {}
            if payload.get("enabled", True) is not False:
                return ws.id
            break
    raise _Refusal(f"the mailbox capability is not enabled for workspace {ws.id!r}", 403)


def _authorize(request: Request) -> tuple[str, str | None]:
    session, org = _caller(request)
    _require_enabled(session)
    return session, org


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _filters(q) -> dict:
    return {
        "after_uid": _int(q.get("after_uid"), 0),
        "to": q.get("to") or None,
        "sender": q.get("from") or None,
        "subject": q.get("subject") or None,
        "text": q.get("text") or None,
        "newer_than": _int(q.get("newer_than"), 0),
    }


async def _run(request: Request, fn):
    try:
        _session, org = await asyncio.to_thread(_authorize, request)
    except _Refusal as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    try:
        cfg = await asyncio.to_thread(api.MailboxConfig.resolve, org)
        return JSONResponse(await asyncio.to_thread(fn, cfg))
    except api.MailboxError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def get_messages(request: Request) -> JSONResponse:
    """GET /api/mailbox/messages?limit&after_uid&to&from&subject&text&newer_than"""
    q = request.query_params
    return await _run(request, lambda cfg: api.list_messages(
        cfg, limit=_int(q.get("limit"), 20), **_filters(q)))


async def get_message(request: Request) -> JSONResponse:
    """GET /api/mailbox/message/{uid} -> text, links, likely codes, attachment names."""
    uid = _int(request.path_params["uid"], -1)
    if uid < 1:
        return JSONResponse({"error": "uid must be a positive integer"}, status_code=400)
    return await _run(request, lambda cfg: api.read_message(cfg, uid))


async def get_wait(request: Request) -> JSONResponse:
    """GET /api/mailbox/wait?timeout&<filters> -> held up to 55 s for a match."""
    q = request.query_params
    return await _run(request, lambda cfg: api.wait_for(
        cfg, timeout=_int(q.get("timeout"), 50), **_filters(q)))


async def get_probe(request: Request) -> JSONResponse:
    return await _run(request, api.probe)


async def _execute_email_send(row: dict, _decision: dict) -> dict:
    """Post-approval executor for ``kind=email_send``: sends exactly the
    message the operator approved, from the approval session's org mailbox
    (the org derived server-side, never the request body's)."""
    req = row.get("request") or {}
    org = approvals_routes._org_for_approval(row.get("id"))

    def run() -> dict:
        try:
            # The operator's approval is necessary but not sufficient: the
            # requesting session's workspace must also have the capability.
            _require_enabled(str(row.get("session") or ""))
        except _Refusal as e:
            return {"ok": False, "error": str(e)}
        try:
            cfg = api.MailboxConfig.resolve(org)
            return {"ok": True, **api.send_message(
                cfg, to=req.get("to", ""), subject=req.get("subject", ""),
                body=req.get("body", ""), cc=req.get("cc", ""))}
        except api.MailboxError as e:
            return {"ok": False, "error": str(e)}

    return await asyncio.to_thread(run)


approvals_routes.EXECUTORS["email_send"] = _execute_email_send


ROUTES = [
    Route("/api/mailbox/messages", get_messages, methods=["GET"]),
    Route("/api/mailbox/message/{uid}", get_message, methods=["GET"]),
    Route("/api/mailbox/wait", get_wait, methods=["GET"]),
    Route("/api/mailbox/probe", get_probe, methods=["GET"]),
]
