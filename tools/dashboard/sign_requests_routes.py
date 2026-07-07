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
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from tools.dashboard.dao import sign_requests as sr

# Importing the schema module registers the ``autonomy.commit.signing-key#1``
# Setting schema at startup (its SettingSchema self-registers on import). Done
# here rather than in tools/graph/schemas/__init__.py to avoid touching that file
# while unrelated work sits uncommitted in it.
from tools.graph.schemas import commit_signing_key as _sign_key_schema  # noqa: F401

# The per-org signing key lives in this Setting as a passphrase-encrypted armored
# private key. The browser fetches it, decrypts locally, and signs; the server
# only ever holds (and serves) the ENCRYPTED blob.
SIGN_KEY_SET_ID = _sign_key_schema.SIGN_KEY_SET_ID


async def get_sign_key(request: Request) -> PlainTextResponse:
    """GET /api/sign-key -> the org's passphrase-encrypted armored private key,
    or 404 if none is configured. Read-only; serves an already-encrypted value."""
    org = request.query_params.get("org") or None
    try:
        from tools.graph import ops as graph_ops
        members = graph_ops.read_set(SIGN_KEY_SET_ID, org=org)
        for m in (getattr(members, "members", []) or []):
            payload = m.payload if isinstance(m.payload, dict) else {}
            armored = payload.get("armored_private_key") or payload.get("armored")
            if isinstance(armored, str) and "PRIVATE KEY" in armored:
                return PlainTextResponse(armored)
    except Exception:
        pass
    return PlainTextResponse("no signing key configured", status_code=404)


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


def _tree_and_parent(payload: bytes) -> tuple[str | None, str | None]:
    """Pull the tree SHA and first parent SHA out of the commit payload headers
    (headers end at the first blank line)."""
    tree = parent = None
    for line in payload.decode("utf-8", "replace").splitlines():
        if line == "":
            break
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent ") and parent is None:
            parent = line[7:].strip()
    return tree, parent


async def get_sign_request(request: Request) -> JSONResponse:
    """GET /api/sign-requests/{id} -> the bytes to sign + signature state + diff.

    ``payload_b64`` is base64 so the browser recovers the EXACT bytes to sign
    (byte-exactness is the property GitHub's Verified check depends on).
    ``signature`` is null (pending), '' (declined), or the armored signature.
    ``files``/``patch`` are the live diff-tree of the pending commit (parent -> tree)
    so the existing commit overlay can render it; empty if the worktree/tree is gone."""
    r = sr.get(request.path_params["id"])
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = r["payload"]
    tree, parent = _tree_and_parent(payload)
    files, patch = [], ""
    if tree:
        try:
            from agents.workspace_manager import sign_request_diff
            d = sign_request_diff(r["session"], r["repo"], parent, tree)
            files, patch = d["files"], d["patch"]
        except Exception:
            pass  # worktree/tree unavailable (e.g. agent moved on) -> render without diff
    return JSONResponse({
        "id": r["id"],
        "session": r["session"],
        "repo": r["repo"],
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "signature": r["signature"],
        "files": files,
        "patch": patch,
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
    Route("/api/sign-key", get_sign_key, methods=["GET"]),
]
