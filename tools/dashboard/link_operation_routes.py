"""The operator's own share-link publish and revoke (auto-fkhq0.10a).

When the operator publishes or revokes a link in their own browser (an
organization invitation, sharing an asset, a Fleet invitation), the requester
IS the decider, so there is no approval to make, only the review dialog they
confirm before signing. Two strict operator-only steps, on the same code path
an approved Central request uses (link_operations.py):

- ``POST /api/links/operations`` ``{"op": "publish" | "revoke", "request": {...}}``
  validates and freezes the request (the same planner) and returns
  ``{operation_id, review, signing}``: what the dialog shows and what it signs.
- ``POST /api/links/operations/{operation_id}`` ``{"envelope": ...[, "ttl": ...]}``
  verifies the envelope (signed no earlier than the prepare) and carries the
  operation out once. A repeat returns the recorded result.

Both steps require the operator (operator_mutation_guard: the operator
principal and same-origin). A ``graph session-auth`` dashboard:ui session
passes that guard, but it cannot sign an envelope (that needs the org session
key in the operator's browser), so it cannot publish.

Each journal entry records ``initiator: "operator"``, the signing persona and
the grant id, keyed by a server-generated operation id, so this is the audit
record of an operator-initiated link.
"""

from __future__ import annotations

import asyncio
import secrets
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import attention_routes
from tools.dashboard import link_operations as ops

#: A prepared operation not signed within this window is gone.
PREPARED_WINDOW_SECONDS = 1800
_STATUS = {
    "invalid_request": 422, "not_found": 404, "stale_envelope": 409,
    "authority_refused": 409, "running": 409, "window_closed": 409, "unavailable": 503,
}


def _no_store(body: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers={"Cache-Control": "no-store"})


def _refusal(exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", "unavailable")
    if code not in _STATUS:
        code = "unavailable"
    body = {"error": code}
    detail = getattr(exc, "detail", None)
    if code in ("authority_refused", "invalid_request") and isinstance(detail, str):
        body["detail"] = detail[:500]
    return _no_store(body, status_code=_STATUS[code])


def prepare(body: object, *, now: float | None = None) -> dict:
    if not isinstance(body, dict) or set(body) != {"op", "request"} \
            or body.get("op") not in (ops.PUBLISH, ops.REVOKE):
        raise ops.LinkOperationError("invalid_request")
    planned = ops.plan(body["op"], body["request"])
    current = time.time() if now is None else now
    # A dialog opened and closed without signing leaves a prepared entry;
    # prune those past the window here rather than keep them forever.
    for stale in ops.Journal.stale_prepared(current - PREPARED_WINDOW_SECONDS):
        ops.Journal.delete(stale)
    operation_id = "op-" + secrets.token_hex(16)
    ops.prepare_entry(operation_id, op=body["op"], initiator="operator", planned=planned,
                      now=current)
    return {"operation_id": operation_id, "review": planned["review"],
            "signing": ops.signing_view(planned["request"], planned["staged"])}


async def carry_out(operation_id: str, body: object, *, now: float | None = None) -> dict:
    if not isinstance(operation_id, str) or not operation_id.startswith("op-"):
        raise ops.LinkOperationError("not_found")
    entry = ops.read(operation_id)
    if entry is None or entry.get("initiator") != "operator":
        raise ops.LinkOperationError("not_found")
    if entry.get("state") in ("done", "failed"):
        return {"execution": entry["execution"]}
    if entry.get("state") == "claimed":
        raise ops.LinkOperationError("running")
    current = time.time() if now is None else now
    if current >= float(entry["prepared_at"]) + PREPARED_WINDOW_SECONDS:
        raise ops.LinkOperationError("window_closed")
    decision, persona = ops.verify(entry["op"], entry["request"], entry["staged"], body,
                                   not_before=float(entry["prepared_at"]))
    return {"execution": await ops.execute(operation_id, entry, decision, persona)}


async def api_link_operation_prepare(request: Request) -> JSONResponse:
    denied = attention_routes.operator_mutation_guard(request)
    if denied is not None:
        return denied
    try:
        body = await attention_routes._strict_json_object(request)
    except ValueError:
        return _no_store({"error": "invalid_request"}, status_code=422)
    try:
        return _no_store(await asyncio.to_thread(prepare, body))
    except Exception as exc:
        return _refusal(exc)


async def api_link_operation_carry_out(request: Request) -> JSONResponse:
    denied = attention_routes.operator_mutation_guard(request)
    if denied is not None:
        return denied
    try:
        body = await attention_routes._strict_json_object(request)
    except ValueError:
        return _no_store({"error": "invalid_request"}, status_code=422)
    try:
        return _no_store(await carry_out(request.path_params["operation_id"], body))
    except Exception as exc:
        return _refusal(exc)
    finally:
        body.clear()


ROUTES = [
    Route("/api/links/operations", api_link_operation_prepare, methods=["POST"]),
    Route("/api/links/operations/{operation_id}", api_link_operation_carry_out, methods=["POST"]),
]
