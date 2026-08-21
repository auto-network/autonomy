"""Operator Dashboard API for machine-neutral fleet enrollment.

The browser owns every personal-root signature.  These routes register its
signed invitation, display pending public requests, and verify the two signed
approval records.  They never accept a root seed or signing key.  Approval and
decline require the human Dashboard session cookie; a session bearer can
authenticate an API caller but cannot exercise personal-root authority.
"""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import (
    fleet_enrollment_service,
    identity_routes,
    link_serving,
    unlock_routes,
)
from tools.network import fleet_invite


def _operator_required(request: Request) -> JSONResponse | None:
    if unlock_routes.session_from_request(request) is None:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "fleet enrollment approval requires an unlocked human "
                    "Dashboard session"
                ),
            },
            status_code=401,
        )
    return None


async def register_invite(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    expected = {"org", "target_uuid", "grant_token", "invite"}
    if not isinstance(body, dict) or set(body) != expected:
        return JSONResponse(
            {"ok": False, "error": f"body must carry exactly {sorted(expected)}"},
            status_code=400,
        )
    org = body["org"]
    token = body["grant_token"]
    if not isinstance(org, str) or not org:
        return JSONResponse({"ok": False, "error": "org must be a non-empty slug"}, status_code=400)
    try:
        invite = fleet_invite.FleetInvite.from_dict(body["invite"])
        fleet_invite.verify(invite)
        personal = identity_routes._personal_member()
        anchor = (personal.payload if personal is not None else {}).get("root_pub")
        if not isinstance(anchor, str) or invite.personal_root_pub != anchor:
            raise ValueError("invite is not anchored to the stored personal identity")
        grant = link_serving.check_grant(token, org=org, now=time.time())
        if (
            grant is None
            or grant.get("target_type") != "fleet:join"
            or grant.get("target_uuid") != body["target_uuid"]
        ):
            raise ValueError("grant is unavailable or is not this fleet invitation")
        fleet_enrollment_service.FleetEnrollmentStore().register_invite(
            target_uuid=body["target_uuid"],
            grant_token=token,
            invite=invite,
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({
        "ok": True,
        "target_uuid": body["target_uuid"],
        "invite_id": invite.invite_id,
        "rendezvous": invite.rendezvous,
    })


async def pending_requests(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    target_uuid = request.query_params.get("target_uuid")
    try:
        rows = fleet_enrollment_service.FleetEnrollmentStore().list_pending(
            target_uuid
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({
        "ok": True,
        "requests": [
            {
                "request_id": row.request_id,
                "target_uuid": row.target_uuid,
                "request": row.request.to_dict(),
                "channel_binding": row.channel_binding,
                "verification_code": row.verification_code,
                "status": row.status,
                "source_approval_id": row.source_approval_id,
                "last_error_code": row.last_error_code,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ],
    })


ROUTES = [
    Route("/api/fleet/invitations/register", register_invite, methods=["POST"]),
    Route("/api/fleet/enrollment/requests", pending_requests, methods=["GET"]),
]
