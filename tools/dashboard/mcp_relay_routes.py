"""Relay-facing internal API for the ChatGPT MCP relay.

The relay runs as a separate host process (not a browser), so these `/api/mcp/*`
routes — which the human-gate middleware lets through — authenticate with a
**service token** (``MCP_RELAY_SERVICE_TOKEN`` shared secret, checked per handler
with ``hmac.compare_digest``; fail-closed when unset).

Two endpoints, both POST:
- ``/api/mcp/session/resolve`` — the relay calls this on ``hello`` and to check a
  chat's binding. Creates a pending ``mcp_peer_link`` approval (→ dashboard popup)
  when a session is unknown/expired; reconciles a declined approval to `denied`.
- ``/api/mcp/crosstalk/resolve`` — the relay calls this before a ``crosstalk_send``
  to a specific session; opens a per-``(session, target)`` ``mcp_crosstalk`` popup.

All authorization state lives in ``mcp_relay_db``; approvals ride the generalized
``/api/approvals`` rendezvous. See design note graph://eeb23208-257.
"""

from __future__ import annotations

import hmac
import os
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import mcp_peer_approvals as kinds
from tools.dashboard.dao import approval_requests as ar
from tools.dashboard.dao import mcp_relay_db as db
from tools.dashboard.event_bus import event_bus

SERVICE_TOKEN_ENV = "MCP_RELAY_SERVICE_TOKEN"


def _relay_auth(request: Request) -> JSONResponse | None:
    """Bearer service-token check. Fail-closed: no configured token → 503."""
    expected = os.environ.get(SERVICE_TOKEN_ENV) or ""
    if not expected:
        return JSONResponse(
            {"error": "MCP relay service token not configured on the dashboard"},
            status_code=503)
    header = request.headers.get("Authorization", "")
    presented = header[7:] if header.startswith("Bearer ") else ""
    if not presented or not hmac.compare_digest(presented, expected):
        return JSONResponse({"error": "invalid relay service token"}, status_code=401)
    return None


async def _open_approval(kind: str, session: str, request_payload: dict) -> str:
    """Create a pending approval on the shared rendezvous and notify viewers."""
    prepare = kinds.PREPARE_CREATE.get(kind)
    req, staged = prepare(session, request_payload) if prepare else (request_payload, None)
    rid = ar.create(kind=kind, session=session, request=req, staged=staged,
                    created_at=time.time())
    await event_bus.broadcast("approval:pending",
                              {"id": rid, "kind": kind, "session": session})
    return rid


def _has_open_approval(approval_id: str | None) -> bool:
    """True if the recorded approval exists and is still undecided (so we don't
    spawn a duplicate popup on every poll)."""
    if not approval_id:
        return False
    row = ar.get(approval_id)
    return bool(row) and row.get("result") is None


def _reconcile(osession: str) -> None:
    """Reflect a decided approval into the binding. Approvals are applied by the
    executor; a DECLINE denies the session unless it already holds a live binding
    (a declined re-request must never revoke access the chat already has). Run by
    both resolve (hello) and status (per-request) so either sees a fresh verdict."""
    row = db.get_session(osession)
    if not row or not row.get("approval_id"):
        return
    appr = ar.get(row["approval_id"])
    if appr and appr.get("result") is not None and not appr["result"].get("approved"):
        if db.resolve_session(osession)["status"] != db.APPROVED:
            db.set_session_status(osession, db.DENIED)


async def resolve_session(request: Request) -> JSONResponse:
    err = _relay_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    osession = str(body.get("openai_session") or "").strip()
    if not osession:
        return JSONResponse({"error": "openai_session required"}, status_code=400)
    subject = str(body.get("openai_subject") or "")
    oorg = str(body.get("openai_org") or "")
    intent = str(body.get("intent") or "")

    _reconcile(osession)

    # A hello is always a request. There is no org here — the operator chooses.
    # (Re)establish a pending record when there's no live binding, then ensure
    # exactly one open approval popup (deduped while undecided). A live (approved)
    # session KEEPS its binding; the popup is a re-request the operator can grant
    # (to change org/level) or ignore.
    status = db.resolve_session(osession)["status"]
    if status in ("unknown", "denied", "expired"):
        db.upsert_pending_session(osession, openai_subject=subject,
                                  openai_org=oorg, intent=intent)
    current = db.get_session(osession)
    if not _has_open_approval(current.get("approval_id")):
        rid = await _open_approval(kinds.KIND_LINK, osession, {
            "openai_session": osession, "openai_subject": subject,
            "openai_org": oorg, "intent": intent})
        db.set_session_approval_id(osession, rid)
    return JSONResponse(db.resolve_session(osession))


async def session_status(request: Request) -> JSONResponse:
    """Non-popping per-request authorization check (the relay calls this on every
    tool call). Pure read of the current binding — never opens an approval."""
    err = _relay_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    osession = str(body.get("openai_session") or "").strip()
    if not osession:
        return JSONResponse({"error": "openai_session required"}, status_code=400)
    _reconcile(osession)
    return JSONResponse(db.resolve_session(osession))


async def resolve_crosstalk(request: Request) -> JSONResponse:
    err = _relay_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    osession = str(body.get("openai_session") or "").strip()
    target = str(body.get("target_session") or "").strip()
    target_org = str(body.get("target_org") or "")
    if not osession or not target:
        return JSONResponse({"error": "openai_session and target_session required"},
                            status_code=400)

    # The chat must be a linked (approved) session before it can be granted
    # per-session crosstalk.
    if db.resolve_session(osession)["status"] != db.APPROVED:
        return JSONResponse({"status": "peer_not_linked"})

    # Reconcile a decided crosstalk approval: a decline becomes 'denied' so we
    # stop re-popping it.
    grant = db.get_crosstalk_grant(osession, target)
    if grant and grant.get("status") == db.PENDING and grant.get("approval_id"):
        appr = ar.get(grant["approval_id"])
        if appr and appr.get("result") is not None and not appr["result"].get("approved"):
            db.set_crosstalk_status(osession, target, db.DENIED)
            grant = db.get_crosstalk_grant(osession, target)

    if db.crosstalk_allowed(osession, target):
        return JSONResponse({"status": db.APPROVED})
    if grant and grant.get("status") == db.DENIED:
        return JSONResponse({"status": db.DENIED})

    db.upsert_pending_crosstalk(osession, target, target_org=target_org)
    current = db.get_crosstalk_grant(osession, target)
    if not _has_open_approval(current.get("approval_id")):
        rid = await _open_approval(kinds.KIND_CROSSTALK, osession, {
            "openai_session": osession, "target_session": target, "target_org": target_org})
        db.set_crosstalk_approval_id(osession, target, rid)
    return JSONResponse({"status": db.PENDING})


ROUTES = [
    Route("/api/mcp/session/resolve", resolve_session, methods=["POST"]),
    Route("/api/mcp/session/status", session_status, methods=["POST"]),
    Route("/api/mcp/crosstalk/resolve", resolve_crosstalk, methods=["POST"]),
]
