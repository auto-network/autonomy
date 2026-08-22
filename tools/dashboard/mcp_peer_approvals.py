"""Approval kinds for the ChatGPT MCP relay — one more pair of kinds on the
generalized approval rendezvous (see approvals_routes.py), not a parallel system.

- ``mcp_peer_link``: binds one ``openai/session`` (per-chat, tunnel-stamped) to
  exactly one Autonomy org at a level (read | readwrite) for a TTL. Fires when a
  ChatGPT chat says ``hello``.
- ``mcp_crosstalk``: a per-``(openai/session, target_session)`` sticky grant to
  message a specific session, TTL'd. Fires the first time that chat tries to
  CrossTalk that session; also the explicit cross-org gate.

The stored request carries the OpenAI identity the relay extracted from the
mTLS-verified transport — the operator sees it but it is not operator-editable.
The operator's decision supplies the Autonomy org, level, and TTL. See design
note graph://eeb23208-257.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from starlette.requests import Request

from tools.dashboard.dao import auth_db
from tools.dashboard.dao import mcp_relay_db

KIND_LINK = "mcp_peer_link"
KIND_CROSSTALK = "mcp_crosstalk"

_LINK_REQUIRED = {"openai_session", "openai_subject", "openai_org", "intent"}
_CROSSTALK_REQUIRED = {"openai_session", "target_session", "message"}
_CROSSTALK_OPTIONAL = ("target_org", "handle", "intent")


def _ttl_expires_at(decision: dict) -> float | None:
    """ttl_seconds -> absolute expiry (None = never)."""
    ttl = decision.get("ttl_seconds")
    if ttl in (None, "", "none"):
        return None
    ttl = float(ttl)
    if ttl <= 0:
        return None
    return time.time() + ttl


def prepare_create_link(session: str, request: dict) -> tuple[dict, dict]:
    missing = _LINK_REQUIRED - set(request)
    if missing:
        raise ValueError(f"mcp_peer_link request missing: {sorted(missing)}")
    if not str(request.get("intent") or "").strip():
        raise ValueError("mcp_peer_link requires a non-empty intent")
    frozen = {k: str(request.get(k, "")) for k in _LINK_REQUIRED}
    if request.get("handle"):  # short display handle (sess_tag), not the raw session
        frozen["handle"] = str(request["handle"])
    return frozen, {}


def enrich_link(row: dict) -> dict:
    """Attach the operator's org list for the popup dropdown, at GET time so it's
    always current. The org list is DASHBOARD-side only — the requester never sees
    or names orgs; the operator picks. This is what makes the org <select> render."""
    try:
        from tools.graph import org_ops
        orgs = []
        for ref in org_ops.list_orgs():
            slug = ref.get("slug") if isinstance(ref, dict) else getattr(ref, "slug", None)
            if slug:
                orgs.append(slug)
    except Exception:
        orgs = []
    return {"orgs": orgs}


def prepare_create_crosstalk(session: str, request: dict) -> tuple[dict, dict]:
    missing = _CROSSTALK_REQUIRED - set(request)
    if missing:
        raise ValueError(f"mcp_crosstalk request missing: {sorted(missing)}")
    if not str(request.get("message") or "").strip():
        raise ValueError("mcp_crosstalk requires the message being sent")
    frozen = {k: str(request.get(k, "")) for k in _CROSSTALK_REQUIRED}
    for k in _CROSSTALK_OPTIONAL:
        if request.get(k):
            frozen[k] = str(request[k])
    return frozen, {}


def enrich_crosstalk(row: dict) -> dict:
    """Attach the target session's human title so the operator recognises WHO the
    message goes to (recognising the target is the whole decision). Rendered at
    GET time from the live dashboard session row; empty if the target is unknown."""
    target = (row.get("request") or {}).get("target_session") or ""
    label = ""
    try:
        from tools.dashboard.dao import dashboard_db
        sess = dashboard_db.get_session(target)
        if sess:
            label = sess.get("label") or ""
    except Exception:
        label = ""
    return {"target_label": label}


def _require_operator(request: Request, _row: dict, _decision: dict) -> str | None:
    """Only a human-origin, unlocked operator session may approve these grants
    (they hand real graph access to an external client). No-op when the unlock
    gate is disabled. Mirrors dashboard_access_approvals.authorize_decision."""
    from tools.dashboard import unlock_routes

    if unlock_routes.gate_disabled():
        return None
    session = unlock_routes.session_from_request(request)
    if session is None or session.get("method") not in {"bootstrap", "passkey", "password"}:
        return "unlock the dashboard before approving MCP access"
    return None


def mint_peer_bearer(
    openai_session: str, autonomy_org: str | None, handle: str | None,
) -> str | None:
    """Mint + persist a fresh ORG-SCOPED bearer for an approved chat, rotating any
    prior token for its handle. Returns the raw token (also stored on the
    mcp_sessions row via set_session_bearer), or None when there's no org/handle to
    stamp it with. Called at approval AND lazily by the relay's resolve/status the
    first time an approved row is found without a bearer — self-healing for a
    session approved before this path existed."""
    if not handle or not autonomy_org:
        return None
    auth_db.revoke_token(handle)
    raw = secrets.token_urlsafe(32)
    auth_db.insert_token(
        hashlib.sha256(raw.encode()).hexdigest(), handle, org=autonomy_org)
    mcp_relay_db.set_session_bearer(openai_session, raw)
    return raw


async def execute_link(row: dict, decision: dict) -> dict:
    """On approval, bind the session to the chosen org/level/TTL."""
    req = row.get("request") or {}
    openai_session = req.get("openai_session")
    autonomy_org = decision.get("autonomy_org")
    level = decision.get("level")
    if not openai_session:
        return {"ok": False, "error": "approval row is missing openai_session"}
    if not autonomy_org or not isinstance(autonomy_org, str):
        return {"ok": False, "error": "decision must include an autonomy_org"}
    if level not in ("read", "readwrite"):
        return {"ok": False, "error": "decision level must be 'read' or 'readwrite'"}
    try:
        bound = mcp_relay_db.approve_session(
            openai_session, autonomy_org=autonomy_org, level=level,
            expires_at=_ttl_expires_at(decision),
            approved_by=str(decision.get("approved_by") or "operator"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if bound is None:
        return {"ok": False, "error": "no pending session to approve (expired?)"}
    # Mint the chat a real ORG-SCOPED session bearer so its graph-CLI-backed
    # tools (search/read/tail/sessions) authenticate to the GENERAL API scoped to
    # exactly this org — the service token only reaches /api/mcp/*. Identity is
    # the minted handle; org is set, so it classifies as ORG_SESSION and needs no
    # session row. Rotate: revoke any prior token for this handle first. The raw
    # is handed back to the relay via resolve_session/session_status; the reconcile
    # path revokes it the moment the binding stops being live-approved.
    mint_peer_bearer(
        openai_session, autonomy_org,
        bound.get("handle") or mcp_relay_db.ensure_handle(openai_session))
    return {"ok": True, "autonomy_org": autonomy_org, "level": level,
            "expires_at": bound.get("expires_at")}


async def execute_crosstalk(row: dict, decision: dict) -> dict:
    """On approval: write the (chat, target) grant AND deliver the held message.

    The message the operator saw is stored on the approval request; delivering it
    here — rather than handing it back to the relay — is what lets the dashboard
    stamp the source from the chat's minted handle (never the relay's launching
    identity). The handle is read from the authoritative session record, not from
    the request, so a stale/forged request handle cannot change attribution.
    """
    req = row.get("request") or {}
    openai_session = req.get("openai_session")
    target_session = req.get("target_session")
    message = req.get("message") or ""
    if not openai_session or not target_session:
        return {"ok": False, "error": "approval row missing openai_session/target_session"}
    granted = mcp_relay_db.approve_crosstalk(
        openai_session, target_session, expires_at=_ttl_expires_at(decision),
        approved_by=str(decision.get("approved_by") or "operator"))
    if granted is None:
        return {"ok": False, "error": "no pending crosstalk grant to approve"}
    sess = mcp_relay_db.get_session(openai_session)
    handle = (sess or {}).get("handle") or openai_session
    delivery = {}
    if message.strip():
        from tools.dashboard import crosstalk_delivery
        delivery = await crosstalk_delivery.deliver_from_chat(
            handle, target_session, message)
    return {"ok": True, "target_session": target_session, "from": handle,
            "expires_at": granted.get("expires_at"), **delivery}


PREPARE_CREATE = {KIND_LINK: prepare_create_link, KIND_CROSSTALK: prepare_create_crosstalk}
EXECUTORS = {KIND_LINK: execute_link, KIND_CROSSTALK: execute_crosstalk}
AUTHORIZE_DECISION = {KIND_LINK: _require_operator, KIND_CROSSTALK: _require_operator}
ENRICH = {KIND_LINK: enrich_link, KIND_CROSSTALK: enrich_crosstalk}
