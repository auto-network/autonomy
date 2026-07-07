"""Commit-signing rendezvous HTTP routes (the backend half of the signing flow).

Three endpoints over one table. The agent's shim creates a request with the exact
commit bytes and polls it; the operator's browser reads the bytes, signs in-page,
and posts the signature back. No auth: a bogus request just sits unsigned because
the operator won't recognise it. Nothing here signs or assembles.

Deferred to the session-viewer integration (marked below): emit the
``commit_sign_pending`` SSE event on the session's stream, and enrich the GET
response with the live ``git diff-tree`` files/patch so the commit overlay renders.
"""

from __future__ import annotations

import base64
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard.dao import sign_requests as sr


async def create_sign_request(request: Request) -> JSONResponse:
    """POST /api/sign-requests?session=&repo=  body = raw commit bytes -> {id}."""
    session = request.query_params.get("session", "")
    repo = request.query_params.get("repo", "")
    payload = await request.body()
    if not session or not repo or not payload:
        return JSONResponse({"error": "session, repo, and a non-empty body are required"},
                            status_code=400)
    rid = sr.create(session=session, repo=repo, payload=payload, created_at=time.time())
    # TODO(viewer-integration): emit SSE {commit_sign_pending: rid} on session's stream
    return JSONResponse({"id": rid})


async def get_sign_request(request: Request) -> JSONResponse:
    """GET /api/sign-requests/{id} -> the bytes to sign + signature state.

    ``payload_b64`` is base64 so the browser recovers the EXACT bytes to sign
    (byte-exactness is the property GitHub's Verified check depends on).
    ``signature`` is null (pending), '' (declined), or the armored signature."""
    r = sr.get(request.path_params["id"])
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "id": r["id"],
        "session": r["session"],
        "repo": r["repo"],
        "payload_b64": base64.b64encode(r["payload"]).decode("ascii"),
        "signature": r["signature"],
        # TODO(viewer-integration): add live diff-tree "files" + "patch" for the overlay
    })


async def submit_signature(request: Request) -> JSONResponse:
    """POST /api/sign-requests/{id}/signature  {armored_signature} — set the result.

    Empty string declines (the shim surfaces that to the agent as a failed commit)."""
    rid = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if sr.get(rid) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    updated = sr.set_signature(rid, body.get("armored_signature", ""))
    # TODO(viewer-integration): emit SSE {commit_sign_pending: null} on session's stream
    return JSONResponse({"ok": updated})


ROUTES = [
    Route("/api/sign-requests", create_sign_request, methods=["POST"]),
    Route("/api/sign-requests/{id}", get_sign_request, methods=["GET"]),
    Route("/api/sign-requests/{id}/signature", submit_signature, methods=["POST"]),
]
