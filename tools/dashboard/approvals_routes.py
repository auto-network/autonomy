"""On-demand operator-approval HTTP routes (the generalized signing rendezvous).

One generic primitive over one table: a requester POSTs a pending request of
some ``kind``, the operator's browser reviews it and POSTs a decision, the
requester consumes the result. Kind-specific behavior lives in small
registries — server-side ``ENRICH`` below for GETs whose stored request isn't
self-describing, and the overlay's per-kind client handlers — never in columns
or per-kind routes. No auth, by explicit operator decision: an agent can
currently approve its own request; the operator-identity gate on the decision
route is a planned follow-up. (For ``commit_sign`` specifically a forged
approval is also harmless — a GPG signature authenticates itself.)

Nothing here polls. The operator's viewer is notified over the SSE event bus
(``approval:pending`` / ``approval:decided``; ``pending_approval`` on the
session detail remains the durable field for reconnect recovery), and the
requester blocks on ``GET /api/approvals/{id}?wait=N`` — held open server-side
until the decision lands or the window elapses.
"""

from __future__ import annotations

import asyncio
import base64
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from tools.dashboard.dao import approval_requests as ar
from tools.dashboard.event_bus import event_bus

# Importing the schema module registers the ``autonomy.commit.signing-key#1``
# Setting schema at startup (its SettingSchema self-registers on import). Done
# here rather than in tools/graph/schemas/__init__.py to avoid touching that file
# while unrelated work sits uncommitted in it.
from tools.graph.schemas import commit_signing_key as _sign_key_schema  # noqa: F401

# The per-org signing key lives in this Setting as a passphrase-encrypted armored
# private key. The browser fetches it, decrypts locally, and signs; the server
# only ever holds (and serves) the ENCRYPTED blob. Kind-specific helper for
# ``commit_sign``.
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


def _enrich_commit_sign(row: dict) -> dict:
    """``files``/``patch``: the live diff-tree of the pending commit (parent ->
    tree) so the commit overlay can render it. Needed because the stored request
    isn't self-describing — the diff lives in the worktree. Empty if the
    worktree/tree is gone (e.g. the agent moved on)."""
    req = row["request"]
    payload = base64.b64decode(req.get("payload_b64", ""))
    tree, parent = _tree_and_parent(payload)
    files, patch = [], ""
    if tree:
        try:
            from agents.workspace_manager import sign_request_diff
            d = sign_request_diff(row["session"], req.get("repo", ""), parent, tree)
            files, patch = d["files"], d["patch"]
        except Exception:
            pass  # worktree/tree unavailable -> render without diff
    return {"files": files, "patch": patch}


# Share-link approval kinds (link_publish / link_revoke) live in their own
# module; it exports plain dicts so this file stays the single registry.
from tools.dashboard import link_approvals as _link_approvals

# Per-kind GET enrichment — the only kind-specific hook on the server side of
# the primitive. A kind whose stored request is self-describing needs no entry.
ENRICH = {
    "commit_sign": _enrich_commit_sign,
    **_link_approvals.ENRICH,
}


# One event per undecided request that something is waiting on. Created lazily
# by waiters, fired + removed by the decision — so the dict is bounded by the
# number of concurrently-awaited pending requests.
_decision_waiters: dict[str, asyncio.Event] = {}

# Per-kind post-approval executors: async (row, decision_body) -> execution
# outcome dict. For a kind registered here, the operator's approval is
# acknowledged immediately and the operation runs as a backend task; the
# single result write happens when it completes — {approved: true,
# execution: {...}} — so the requester's held GET delivers the actual
# outcome, not just the verdict. The decision body is passed through so
# kinds whose approval carries client-produced material (e.g. the signed
# registry envelope for link_publish) can consume it. A decline never
# executes anything. Kinds without an executor (commit_sign: the browser
# itself produces the signature) store the verdict body directly.
EXECUTORS: dict = {
    **_link_approvals.EXECUTORS,
}

# Requests whose executor is running: the verdict is committed but the result
# row is written only on completion, so further decisions must be refused here
# rather than by the result-row first-writer-wins check.
_executing: set[str] = set()


def _finalize_decision(rid: str, kind: str, session: str) -> None:
    """Wake held ?wait= calls and tell viewers the request is closed."""
    ev = _decision_waiters.pop(rid, None)
    if ev:
        ev.set()
    event_bus.broadcast_sync("approval:decided",
                             {"id": rid, "kind": kind, "session": session})

# Cap on ?wait= so a stuck client can't hold a connection open indefinitely;
# requesters (e.g. the signing shim) loop on the held GET instead.
MAX_WAIT_S = 60.0


async def create_approval(request: Request) -> JSONResponse:
    """POST /api/approvals  {kind, session, request} -> {id}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = body.get("kind")
    session = body.get("session")
    req = body.get("request")
    if not kind or not session or not isinstance(req, dict) or not req:
        return JSONResponse(
            {"error": "kind, session, and a non-empty request object are required"},
            status_code=400)
    rid = ar.create(kind=kind, session=session, request=req, created_at=time.time())
    # Push-notify the operator's open viewer(s); pending_approval on the
    # session detail stays the durable fallback for a viewer that (re)connects.
    await event_bus.broadcast("approval:pending",
                              {"id": rid, "kind": kind, "session": session})
    return JSONResponse({"id": rid})


async def get_approval(request: Request) -> JSONResponse:
    """GET /api/approvals/{id}[?wait=N] -> the request + decision state.

    ``result`` is null while pending, else the decision JSON
    ({"approved": bool, ...kind fields, ...operator edits}).

    ``?wait=N`` blocks the requester side of the rendezvous: the response is
    held until the decision is written or N seconds (capped) elapse — the
    no-polling replacement for a client retry loop. Held calls skip the
    kind enrichment: the waiter wants the result, not the review rendering."""
    rid = request.path_params["id"]
    r = ar.get(rid)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    wait = request.query_params.get("wait")
    if wait is not None:
        if r["result"] is None:
            ev = _decision_waiters.setdefault(rid, asyncio.Event())
            try:
                await asyncio.wait_for(ev.wait(), min(float(wait or 0), MAX_WAIT_S))
            except (asyncio.TimeoutError, ValueError):
                pass
            r = ar.get(rid) or r
        return JSONResponse({
            "id": r["id"], "kind": r["kind"], "session": r["session"],
            "request": r["request"], "result": r["result"],
        })
    extra = {}
    enrich = ENRICH.get(r["kind"])
    if enrich:
        try:
            extra = enrich(r) or {}
        except Exception:
            extra = {}
    return JSONResponse({
        "id": r["id"],
        "kind": r["kind"],
        "session": r["session"],
        "request": r["request"],
        "result": r["result"],
        **extra,
    })


async def decide_approval(request: Request) -> JSONResponse:
    """POST /api/approvals/{id}/decision  {approved: bool, ...} — the body IS the
    stored result: the operator's true/false plus kind-specific outputs (e.g. the
    armored ``signature``). The request itself is never modified by a decision —
    what was staged is exactly what an approval applies to. First writer wins."""
    rid = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body.get("approved"), bool):
        return JSONResponse({"error": "decision requires a boolean 'approved'"},
                            status_code=400)
    r = ar.get(rid)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if rid in _executing:
        return JSONResponse({
            "ok": False,
            "error": "This approval is already being processed.",
        })
    if r["result"] is not None:
        return JSONResponse({
            "ok": False,
            "error": "This approval has already been completed.",
        })

    executor = EXECUTORS.get(r["kind"])
    if body["approved"] and executor:
        _executing.add(rid)

        async def run_and_record():
            try:
                outcome = await executor(r, body)
            except Exception as e:  # the requester gets a failure, never a hang
                outcome = {"ok": False, "error": str(e)}
            finally:
                _executing.discard(rid)
            if ar.set_result(rid, {**body, "execution": outcome}):
                _finalize_decision(rid, r["kind"], r["session"])

        asyncio.get_running_loop().create_task(run_and_record())
        return JSONResponse({"ok": True})

    updated = ar.set_result(rid, body)
    if updated:
        _finalize_decision(rid, r["kind"], r["session"])
    return JSONResponse({
        "ok": updated,
        **({} if updated else {
            "error": "This approval has already been completed.",
        }),
    })


ROUTES = [
    Route("/api/approvals", create_approval, methods=["POST"]),
    Route("/api/approvals/{id}", get_approval, methods=["GET"]),
    Route("/api/approvals/{id}/decision", decide_approval, methods=["POST"]),
    Route("/api/sign-key", get_sign_key, methods=["GET"]),
]
