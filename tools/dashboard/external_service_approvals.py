"""Central approval kind for narrowly scoped external service credentials.

Producers own their enrollment transport and pass server-derived concrete API
capabilities into this central kind. A public requester can never choose its
own audience, methods, or paths. The approval row is the durable lifecycle
record; no producer keeps a second approval queue.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from starlette.requests import Request

from tools.dashboard.dao import auth_db


KIND = "external_service_access"
MAX_TTL_SECONDS = 10 * 365 * 24 * 60 * 60

# Registry-owned application identity. The trusted producer supplies concrete
# audience and API pairs as parameters; its public client never does.
APPLICATIONS = {
    "dropbox": {
        "application": "Autonomy Capture",
        "summary": "Add screenshots to the global operator dropbox",
        "token_name_prefix": "dropbox-upload",
    },
}


def _valid_ttl(value: object, *, allow_none: bool = True) -> int | None:
    if value is None and allow_none:
        return None
    if type(value) is not int or value <= 0 or value > MAX_TTL_SECONDS:
        raise ValueError(
            "ttl_seconds must be a positive integer no greater than 10 years, "
            "or null for no expiry"
        )
    return value


def registered_request(
    *,
    application_scope: str,
    source_approval_id: str,
    requester_label: object,
    requested_ttl_seconds: object,
    resource_audience: str,
    capabilities: list[dict[str, str]],
) -> tuple[dict, dict]:
    """Build review and execution data from a server-owned registration."""
    spec = APPLICATIONS.get(application_scope)
    if spec is None:
        raise ValueError("unknown external service application scope")
    label = str(requester_label or "External device").strip()
    if not label or len(label) > 120:
        raise ValueError("requester label must be between 1 and 120 characters")
    requested_ttl = _valid_ttl(requested_ttl_seconds)
    if (
        not isinstance(resource_audience, str)
        or not resource_audience
        or len(resource_audience) > 256
    ):
        raise ValueError("resource audience must be a non-empty string up to 256 characters")
    normalized_capabilities = auth_db.normalize_api_capabilities(capabilities)
    request = {
        "sourceApprovalId": source_approval_id,
        "application_scope": application_scope,
        "requester": {
            "kind": "device_enrollment",
            "id": source_approval_id,
            "label": label,
        },
        "requested_ttl_seconds": requested_ttl,
        "application": spec["application"],
        "summary": spec["summary"],
        "resource_audience": resource_audience,
        "capabilities": normalized_capabilities,
    }
    staged = {
        **request,
        "token_name_prefix": spec["token_name_prefix"],
    }
    return request, staged


def reject_direct_create(_session: str, _request: dict) -> tuple[dict, dict]:
    """Force this kind through a registered producer enrollment route."""
    raise ValueError(
        "external_service_access requests must use a registered enrollment route"
    )


def enrich(row: dict) -> dict:
    return {"staged": row.get("staged")}


def authorize_decision(
    request: Request, _row: dict, _decision: dict,
) -> str | None:
    """Only the unlocked human operator may mint an external credential."""
    from tools.dashboard import unlock_routes

    if unlock_routes.gate_disabled():
        return None
    session = unlock_routes.session_from_request(request)
    if session is None or session.get("method") not in {
        "bootstrap", "passkey", "password",
    }:
        return "unlock the dashboard before approving external service access"
    return None


async def execute(row: dict, decision: dict) -> dict:
    staged = row.get("staged") or {}
    application_scope = staged.get("application_scope")
    spec = APPLICATIONS.get(application_scope)
    if spec is None:
        return {"ok": False, "error": "unregistered external service capability"}
    # The staged envelope came from a registered server producer, never its
    # public client. Validate it again before minting; request JSON is ignored.
    if (
        staged.get("application") != spec["application"]
        or not isinstance(staged.get("resource_audience"), str)
        or not staged["resource_audience"]
        or len(staged["resource_audience"]) > 256
    ):
        return {"ok": False, "error": "external service capability mismatch"}
    try:
        capabilities = auth_db.normalize_api_capabilities(staged.get("capabilities"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        ttl = _valid_ttl(decision.get("ttl_seconds"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    raw = secrets.token_urlsafe(32)
    expires_at = time.time() + ttl if ttl is not None else None
    source_id = str(staged.get("sourceApprovalId") or row["id"])
    name = f"{spec['token_name_prefix']}:{source_id}"
    auth_db.insert_scoped_service_token(
        hashlib.sha256(raw.encode()).hexdigest(),
        name,
        capabilities=capabilities,
        application_scope=application_scope,
        resource_audience=staged["resource_audience"],
        source_approval_id=source_id,
        expires_at=expires_at,
    )
    return {
        "ok": True,
        "token": raw,
        "expires_at": expires_at,
        "sourceApprovalId": source_id,
        "application_scope": application_scope,
        "resource_audience": staged["resource_audience"],
        "capabilities": capabilities,
    }


PREPARE_CREATE = {KIND: reject_direct_create}
ENRICH = {KIND: enrich}
AUTHORIZE_DECISION = {KIND: authorize_decision}
EXECUTORS = {KIND: execute}
