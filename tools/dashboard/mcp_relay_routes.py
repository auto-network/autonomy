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

import asyncio
import hashlib
import hmac
import os
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import crosstalk_delivery
from tools.dashboard import mcp_peer_approvals as kinds
from tools.dashboard import web_push
from tools.dashboard.dao import approval_requests as ar
from tools.dashboard.dao import auth_db
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
    await web_push.register_approval_pending(rid, kind)
    await event_bus.broadcast("approval:pending",
                              {"id": rid, "kind": kind, "session": session})
    return rid


def _handle(osession: str) -> str:
    """A short, comparable, non-reversible display handle for a chat — shown to the
    operator instead of the raw (bearer-equivalent) openai/session."""
    return hashlib.sha256(osession.encode()).hexdigest()[:12]


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
    if not row:
        return
    # Decline reconciliation (needs the approval to read its verdict).
    if row.get("approval_id"):
        appr = ar.get(row["approval_id"])
        if appr and appr.get("result") is not None and not appr["result"].get("approved"):
            if db.resolve_session(osession)["status"] != db.APPROVED:
                db.set_session_status(osession, db.DENIED)
    # The peer's general-API bearer exists only while the binding is live-approved.
    # Any state that is not live-approved (denied, expired, revoked) revokes it
    # here — the relay calls this on every hello and every per-request status, so
    # a lapsed grant loses its token on the next touch. Independent of the
    # approval_id branch above: a revoked/expired grant must lose its token even
    # if there is no decline verdict to read.
    fresh = db.get_session(osession)
    if fresh and fresh.get("peer_bearer") and (
            db.resolve_session(osession)["status"] != db.APPROVED):
        auth_db.revoke_token(fresh.get("handle") or "")
        db.set_session_bearer(osession, None)


def _authorization(osession: str) -> dict:
    """The relay's authorization view, plus the org-scoped bearer while approved.
    The relay caches ``bearer`` per peer and passes it as CROSSTALK_TOKEN when it
    shells out to the graph CLI, so those calls are scoped to the approved org."""
    result = db.resolve_session(osession)
    if result.get("status") == db.APPROVED:
        row = db.get_session(osession)
        bearer = row.get("peer_bearer") if row else None
        if row and not bearer:
            # Self-heal: a session approved before mint-on-approval existed has no
            # bearer. Mint one lazily on the first approved poll so pre-existing
            # (and any future-gap) peers get a working general-API credential
            # without a separate backfill.
            bearer = kinds.mint_peer_bearer(
                osession, result.get("autonomy_org"), (row or {}).get("handle"))
        if bearer:
            result = {**result, "bearer": bearer}
    return result


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
    handle = db.ensure_handle(osession) or _handle(osession)
    if not _has_open_approval(current.get("approval_id")):
        # The approval's `session` field is the MINTED HANDLE (ChatGPT-<datetime>,
        # what the popup shows); the raw openai_session travels in the request for
        # the executor to bind.
        rid = await _open_approval(kinds.KIND_LINK, handle, {
            "openai_session": osession, "openai_subject": subject,
            "openai_org": oorg, "intent": intent, "handle": handle})
        db.set_session_approval_id(osession, rid)
    return JSONResponse(_authorization(osession))


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
    return JSONResponse(_authorization(osession))


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
    message = str(body.get("message") or "")
    intent = str(body.get("intent") or "")
    if not osession or not target:
        return JSONResponse({"error": "openai_session and target_session required"},
                            status_code=400)

    # The chat must be a linked (approved) session before it can be granted
    # per-session crosstalk. Linking is a SEPARATE step — never raise a link
    # dialog off a send; tell the caller to link first.
    if db.resolve_session(osession)["status"] != db.APPROVED:
        return JSONResponse({"status": "peer_not_linked"})

    grant = _reconcile_crosstalk(osession, target)

    if db.crosstalk_allowed(osession, target):
        return JSONResponse({"status": db.APPROVED})
    if grant and grant.get("status") == db.DENIED:
        return JSONResponse({"status": db.DENIED})

    # Open ONE approval carrying the actual message (that's what the operator
    # authorizes). The relay holds the send pending this decision.
    db.upsert_pending_crosstalk(osession, target, target_org=target_org)
    current = db.get_crosstalk_grant(osession, target)
    rid = current.get("approval_id")
    if not _has_open_approval(rid):
        rid = await _open_approval(kinds.KIND_CROSSTALK, _handle(osession), {
            "openai_session": osession, "target_session": target,
            "target_org": target_org, "handle": _handle(osession),
            "message": message, "intent": intent})
        db.set_crosstalk_approval_id(osession, target, rid)
    return JSONResponse({"status": db.PENDING, "approval_id": rid})


def _reconcile_crosstalk(osession: str, target: str) -> dict | None:
    """A declined crosstalk approval becomes 'denied' so it stops re-popping."""
    grant = db.get_crosstalk_grant(osession, target)
    if grant and grant.get("status") == db.PENDING and grant.get("approval_id"):
        appr = ar.get(grant["approval_id"])
        if appr and appr.get("result") is not None and not appr["result"].get("approved"):
            db.set_crosstalk_status(osession, target, db.DENIED)
            grant = db.get_crosstalk_grant(osession, target)
    return grant


async def crosstalk_status(request: Request) -> JSONResponse:
    """Non-popping poll of a (session, target) grant — the relay calls this in a
    loop while HOLDING a crosstalk_send, to learn approve/decline without opening
    another popup."""
    err = _relay_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    osession = str(body.get("openai_session") or "").strip()
    target = str(body.get("target_session") or "").strip()
    if not osession or not target:
        return JSONResponse({"error": "openai_session and target_session required"},
                            status_code=400)
    grant = _reconcile_crosstalk(osession, target)
    if db.crosstalk_allowed(osession, target):
        return JSONResponse({"status": db.APPROVED})
    if grant and grant.get("status") == db.DENIED:
        return JSONResponse({"status": db.DENIED})
    return JSONResponse({"status": db.PENDING if grant else "none"})


async def relay_crosstalk(request: Request) -> JSONResponse:
    """Single enforce-and-deliver endpoint for a chat messaging a session. The
    relay forwards {openai_session (from), target_session (to), message, intent};
    the dashboard authorizes and delivers, stamping the source from the chat's
    minted handle — the relay never delivers and never sets the source.

    - live (chat, target) grant  -> deliver now, return `delivered`
    - declined earlier           -> return `denied`
    - otherwise                  -> store the message on ONE approval and return
      `pending`; the operator's approval delivers it (execute_crosstalk). The
      relay does not resend — the held message is delivered on approval.
    """
    err = _relay_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    osession = str(body.get("openai_session") or "").strip()
    to = str(body.get("target_session") or "").strip()
    message = str(body.get("message") or "")
    intent = str(body.get("intent") or "")
    target_org = str(body.get("target_org") or "")
    if not osession or not to or not message.strip():
        return JSONResponse(
            {"error": "openai_session, target_session and message required"},
            status_code=400)

    # The chat must be linked (a separate approval) before it can message anyone.
    if db.get_session(osession) is None or \
            db.resolve_session(osession)["status"] != db.APPROVED:
        return JSONResponse({"status": "peer_not_linked"})
    handle = db.ensure_handle(osession) or _handle(osession)

    _reconcile_crosstalk(osession, to)
    if db.crosstalk_allowed(osession, to):
        result = await crosstalk_delivery.deliver_from_chat(handle, to, message)
        return JSONResponse({"status": "delivered", "from": handle, **result})
    grant = db.get_crosstalk_grant(osession, to)
    if grant and grant.get("status") == db.DENIED:
        return JSONResponse({"status": db.DENIED, "from": handle})

    # Reuse an already-open approval (a re-relay of the same undecided send must
    # not raise a second popup). Only when none is open do we (re)set the pending
    # grant and open one — note upsert clears approval_id, so it must precede the
    # set below, never run on the reuse path.
    rid = grant.get("approval_id") if grant else None
    if not _has_open_approval(rid):
        db.upsert_pending_crosstalk(osession, to, target_org=target_org)
        rid = await _open_approval(kinds.KIND_CROSSTALK, handle, {
            "openai_session": osession, "target_session": to,
            "target_org": target_org, "handle": handle,
            "message": message, "intent": intent})
        db.set_crosstalk_approval_id(osession, to, rid)
    return JSONResponse({"status": db.PENDING, "approval_id": rid, "from": handle})


async def collect_crosstalk(request: Request) -> JSONResponse:
    """A chat drains its own inbox — the queued replies addressed to its handle.
    Authorized by the relay's service token plus the chat's authenticated
    openai_session; the dashboard maps that session to its handle and returns ONLY
    that handle's messages, marking them delivered (idempotent: a repeat returns
    nothing new)."""
    err = _relay_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    osession = str(body.get("openai_session") or "").strip()
    try:
        limit = min(int(body.get("limit") or 100), 500)
    except (TypeError, ValueError):
        limit = 100
    if not osession:
        return JSONResponse({"error": "openai_session required"}, status_code=400)
    # A handle is minted on the FIRST hello, before any approval, so without this
    # an unapproved/pending/denied chat could drain its own outbox. Match the
    # sibling routes (resolve_crosstalk, relay_crosstalk): no drain until linked.
    _reconcile(osession)
    if db.resolve_session(osession)["status"] != db.APPROVED:
        return JSONResponse({"status": "peer_not_linked"})
    handle = db.ensure_handle(osession)
    if handle is None:
        return JSONResponse({"messages": []})
    msgs = await asyncio.to_thread(auth_db.collect_inbox, handle, limit)
    return JSONResponse({"handle": handle, "messages": [
        {"from": m["sender_session"], "label": m["sender_label"],
         "message": m["message"], "timestamp": m["timestamp"]} for m in msgs]})


ROUTES = [
    Route("/api/mcp/session/resolve", resolve_session, methods=["POST"]),
    Route("/api/mcp/session/status", session_status, methods=["POST"]),
    Route("/api/mcp/crosstalk/resolve", resolve_crosstalk, methods=["POST"]),
    Route("/api/mcp/crosstalk/status", crosstalk_status, methods=["POST"]),
    Route("/api/mcp/crosstalk/relay", relay_crosstalk, methods=["POST"]),
    Route("/api/mcp/crosstalk/collect", collect_crosstalk, methods=["POST"]),
]
