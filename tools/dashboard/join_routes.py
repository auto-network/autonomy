"""Loopback handoff for joining an existing node to another organization."""

from __future__ import annotations

import asyncio
import ipaddress

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else None
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def post_join_handoff(request: Request) -> JSONResponse:
    """Accept one user-carried invite on an already-identified local node.

    The body carries only the invitation. The personal password is read from
    the same mounted file as headless first-run, never from HTTP or server
    stdin. The E2E transport remains pinned to the root in the invitation.
    """
    if not _is_loopback(request):
        return JSONResponse(
            {"status": "rejected", "reason": "loopback-only"},
            status_code=403,
        )
    # JSON is intentionally non-simple CORS content. A hostile web page
    # cannot smuggle a no-cors text/plain/form POST into this loopback-only
    # mutator; browsers must preflight application/json, and the dashboard
    # grants no cross-origin permission.
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != "application/json":
        return JSONResponse(
            {"status": "rejected", "reason": "application-json-required"},
            status_code=415,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"status": "rejected", "reason": "body-must-be-json"},
            status_code=400,
        )
    if not isinstance(body, dict) or set(body) != {"invite"}:
        return JSONResponse(
            {
                "status": "rejected",
                "reason": "body-must-carry-only-invite",
            },
            status_code=400,
        )

    from tools.init.join import (
        JoinError,
        join_existing_identity,
        persist_outcome,
        personal_identity_exists,
        production_transport,
        read_personal_password,
    )
    from tools.network.invitation import InvitationError, decode_invitation

    try:
        invitation = decode_invitation(body["invite"])
    except InvitationError as exc:
        return JSONResponse(
            {"status": "rejected", "reason": "invalid-invitation", "detail": str(exc)},
            status_code=400,
        )
    if not personal_identity_exists():
        return JSONResponse(
            {
                "status": "rejected",
                "reason": "identity-required",
                "detail": "complete personal identity setup before handoff",
            },
            status_code=409,
        )
    try:
        # HTTP must never become a password ingress. stdin_ok=False also keeps
        # the long-running dashboard from consuming an unrelated process fd.
        password = read_personal_password(stdin_ok=False)
    except JoinError as exc:
        return JSONResponse(
            {"status": "rejected", "reason": "needs-password", "detail": str(exc)},
            status_code=409,
        )
    if password is None:
        return JSONResponse(
            {
                "status": "rejected",
                "reason": "needs-password",
                "detail": (
                    "mount AUTONOMY_PERSONAL_PASSWORD_FILE to unlock the "
                    "existing identity; passwords are never accepted over HTTP"
                ),
            },
            status_code=409,
        )
    try:
        outcome = await asyncio.to_thread(
            join_existing_identity,
            invitation,
            production_transport(invitation),
            password=password,
        )
        persist_outcome(outcome)
    except JoinError as exc:
        return JSONResponse(
            {"status": "rejected", "reason": "join-refused", "detail": str(exc)},
            status_code=400,
        )
    return JSONResponse({
        "status": outcome.state,
        "org": outcome.org,
        "invite_ref": outcome.invite_ref,
        "persona_pub": outcome.persona_pub,
        "role": outcome.granted_role,
        "have": outcome.have,
        "need": outcome.need,
    })


ROUTES = [
    Route("/api/identity/join", post_join_handoff, methods=["POST"]),
]
