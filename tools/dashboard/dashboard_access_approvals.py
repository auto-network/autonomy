"""Signed one-time grants for headless Dashboard UI access.

This is one more kind on the generalized approval rendezvous, not a parallel
approval system. The requester's only authority is an ephemeral Ed25519 public
key. Server policy freezes the scope and two-hour lifetime before the operator
sees or signs the grant.
"""

from __future__ import annotations

import re
import secrets
import time

from starlette.requests import Request

from tools.dashboard.dao import identity_sessions
from tools.dashboard.identity_routes import _personal_member
from tools.network.idkit.armor import parse_armor
from tools.network.idkit.canonical import canonical_json
from tools.network.idkit.errors import IdkitError
from tools.network.idkit.keys import load_public_key, verify_signature


KIND = "dashboard_access"
SCOPE = ["dashboard:ui"]
GRANT_TTL_S = 2 * 60 * 60
GRANT_SIGNING_DOMAIN = b"autonomy.identity.dashboard-access-grant.v1\n"
REDEEM_SIGNING_DOMAIN = b"autonomy.identity.dashboard-access-redeem.v1\n"
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")


def valid_nonce(value: object) -> bool:
    return isinstance(value, str) and _NONCE_RE.fullmatch(value) is not None


def prepare_create(session: str, request: dict) -> tuple[dict, dict]:
    """Validate requester input and return (stored request, frozen grant)."""
    if set(request) != {"ephemeral_pub"}:
        raise ValueError("dashboard access request must carry only 'ephemeral_pub'")
    ephemeral_pub = request.get("ephemeral_pub")
    try:
        load_public_key(ephemeral_pub)
    except IdkitError as exc:
        raise ValueError(f"ephemeral_pub is not a valid Ed25519 key: {exc}") from exc
    if not isinstance(session, str) or not session or len(session) > 256:
        raise ValueError("session must be a non-empty string up to 256 characters")
    issued_at = int(time.time())
    grant = {
        "v": 1,
        "nonce": secrets.token_hex(32),
        "grantee": session,
        "ephemeral_pub": ephemeral_pub,
        "scope": list(SCOPE),
        "issued_at": issued_at,
        "expires_at": issued_at + GRANT_TTL_S,
    }
    return {"ephemeral_pub": ephemeral_pub}, grant


def enrich(row: dict) -> dict:
    """Expose exactly the server-frozen grant the operator must sign."""
    return {"staged": row.get("staged")}


def authorize_decision(request: Request, _row: dict, _decision: dict) -> str | None:
    """Require the decision to come from a human-origin operator session."""
    # Runtime import avoids an import cycle: unlock_routes consumes this
    # module's signing domains for the redemption endpoint.
    from tools.dashboard import unlock_routes

    if unlock_routes.gate_disabled():
        return None
    session = unlock_routes.session_from_request(request)
    if session is None or session.get("method") not in {
        "bootstrap", "passkey", "password",
    }:
        return "unlock the dashboard before deciding this access request"
    return None


def _personal_root_pub() -> str:
    personal = _personal_member()
    if personal is None or not isinstance(personal.payload, dict):
        raise ValueError("no personal identity is stored")
    root_pub = personal.payload.get("root_pub")
    if root_pub:
        return root_pub
    armor = personal.payload.get("armored_private_key")
    if not armor:
        raise ValueError("the personal identity has no signing key")
    try:
        return parse_armor(armor)["root_pub"]
    except IdkitError as exc:
        raise ValueError(f"the personal identity public key is unreadable: {exc}") from exc


async def execute(row: dict, decision: dict) -> dict:
    """Verify the personal-root decision and make its grant redeemable."""
    if set(decision) != {"approved", "grant", "signature"}:
        return {"ok": False, "error": (
            "dashboard access approval must carry only approved, grant, "
            "and signature"
        )}
    grant = decision.get("grant")
    signature = decision.get("signature")
    staged = row.get("staged")
    if not isinstance(grant, dict) or grant != staged:
        return {"ok": False,
                "error": "the signed grant does not match the server-frozen request"}
    if not isinstance(signature, str):
        return {"ok": False, "error": "the approval signature is required"}
    if grant.get("expires_at", 0) <= time.time():
        return {"ok": False, "error": "the dashboard access request has expired"}
    try:
        verify_signature(
            _personal_root_pub(), signature,
            GRANT_SIGNING_DOMAIN + canonical_json(grant),
        )
    except (IdkitError, ValueError):
        return {"ok": False, "error": "the approval signature does not verify"}
    try:
        identity_sessions.store_access_grant(
            nonce=grant["nonce"], approval_id=row["id"],
            ephemeral_pub=grant["ephemeral_pub"],
            operator_signature=signature, grantee=grant["grantee"],
            scope=grant["scope"], issued_at=grant["issued_at"],
            expires_at=grant["expires_at"], approved_at=time.time(),
        )
    except (ValueError, identity_sessions.SessionStoreError) as exc:
        return {"ok": False,
                "error": f"could not store the approved access grant: {exc}"}
    return {"ok": True}


PREPARE_CREATE = {KIND: prepare_create}
ENRICH = {KIND: enrich}
EXECUTORS = {KIND: execute}
AUTHORIZE_DECISION = {KIND: authorize_decision}
