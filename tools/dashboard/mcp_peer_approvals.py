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

import time

from starlette.requests import Request

from tools.dashboard.dao import mcp_relay_db

KIND_LINK = "mcp_peer_link"
KIND_CROSSTALK = "mcp_crosstalk"

_LINK_REQUIRED = {"openai_session", "openai_subject", "openai_org", "intent", "requested_org"}
_CROSSTALK_REQUIRED = {"openai_session", "target_session", "target_org"}


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
    frozen = {k: str(request.get(k, "")) for k in _LINK_REQUIRED}
    return frozen, {}


def prepare_create_crosstalk(session: str, request: dict) -> tuple[dict, dict]:
    missing = _CROSSTALK_REQUIRED - set(request)
    if missing:
        raise ValueError(f"mcp_crosstalk request missing: {sorted(missing)}")
    frozen = {k: str(request.get(k, "")) for k in _CROSSTALK_REQUIRED}
    return frozen, {}


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
    return {"ok": True, "autonomy_org": autonomy_org, "level": level,
            "expires_at": bound.get("expires_at")}


async def execute_crosstalk(row: dict, decision: dict) -> dict:
    req = row.get("request") or {}
    openai_session = req.get("openai_session")
    target_session = req.get("target_session")
    if not openai_session or not target_session:
        return {"ok": False, "error": "approval row missing openai_session/target_session"}
    granted = mcp_relay_db.approve_crosstalk(
        openai_session, target_session, expires_at=_ttl_expires_at(decision),
        approved_by=str(decision.get("approved_by") or "operator"))
    if granted is None:
        return {"ok": False, "error": "no pending crosstalk grant to approve"}
    return {"ok": True, "target_session": target_session,
            "expires_at": granted.get("expires_at")}


PREPARE_CREATE = {KIND_LINK: prepare_create_link, KIND_CROSSTALK: prepare_create_crosstalk}
EXECUTORS = {KIND_LINK: execute_link, KIND_CROSSTALK: execute_crosstalk}
AUTHORIZE_DECISION = {KIND_LINK: _require_operator, KIND_CROSSTALK: _require_operator}
ENRICH: dict = {}  # request is self-describing; the popup reads r.request directly
