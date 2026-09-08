"""On-demand operator-approval HTTP routes (the generalized signing rendezvous).

One generic primitive over one table: a requester POSTs a pending request of
some ``kind``, the operator's browser reviews it and POSTs a decision, the
requester consumes the result. Kind-specific behavior lives in small
registries — server-side ``ENRICH`` below for GETs whose stored request isn't
self-describing, and the overlay's per-kind client handlers — never in columns
or per-kind routes. There is no blanket auth requirement because existing
self-authenticating kinds such as ``commit_sign`` validate their resulting
signature. Sensitive kinds may register a decision authorizer;
``dashboard_access`` requires a live operator session as well as a
personal-root signature.

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

from tools.dashboard import api_auth
from tools.dashboard import attention_routes
from tools.dashboard import session_notify
from tools.dashboard import web_push
from tools.dashboard.approval_http_bridge import (
    CENTRAL_APPROVAL_ID_PREFIX,
    ApprovalHttpBridgeError,
    has_central_approval_prefix,
)
from tools.dashboard.approval_service import (
    ApprovalServiceError,
    resolve_human_approval_actor,
)
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


def _org_for_approval(rid: str) -> str | None:
    """The organization a pending approval belongs to.

    Derived from the request row the operator is already looking at, never
    from anything the browser says. The signing key is per organization, and
    the caller with authority over all of them -- the operator, holding the
    cookie -- is the one caller that carries no organization of its own. So
    the organization has to come from the thing being signed.

    Taking it from a query parameter instead would put a selector for
    somebody's private key under browser control, which
    ``require_global_api_authority`` explicitly tells handlers not to do.
    The approval id is not a selector: it names a row the server owns and
    the operator has already been shown.
    """
    row = ar.get(rid)
    if not row:
        return None
    session = row.get("session")
    if not session:
        return None
    try:
        from tools.dashboard.dao import dashboard_db
        from tools.dashboard.org_identity import session_org_slug
        resolved = session_org_slug(dashboard_db.get_session(session) or {}) or None
        if resolved:
            return resolved
        # Organization Settings is an operator-owned surface rather than an
        # agent session. Its share controls still use the ordinary approval
        # ceremony, whose stored request is the server-visible source of the
        # selected organization. The operator remains the only decider.
        if session == "dashboard-ui":
            requested = (row.get("request") or {}).get("org")
            return requested if isinstance(requested, str) and requested else None
        return None
    except Exception:
        return None


async def get_sign_key(request: Request) -> PlainTextResponse:
    """GET /api/sign-key -> the operator's passphrase-encrypted armored private
    commit-signing key for their organization, or 404 if none is configured.
    Read-only; serves an already-encrypted value.

    The key is the operator's own secret -- decrypted only in their browser,
    with their passphrase, to sign their commits -- so it lives in their own
    database and never on any organization's cross-org read-through surface.
    They hold one per organization they sign for, keyed by its slug, which is
    why the organization is named here rather than guessed. ``peers=[]``
    because a signing key is never read from anyone else's database.
    """
    auth_error = api_auth.require_global_api_authority(request)
    if auth_error is not None:
        return auth_error

    from tools.graph import settings_ops

    # Which key, from the thing being signed. The operator's browser sends no
    # organization -- it has none, it owns them all -- so resolving the caller
    # returned nothing and this took its 404 branch before looking anything
    # up. Four signing attempts failed that way, each reported to the operator
    # as "no signing key is configured", which named the wrong problem.
    approval_id = request.query_params.get("approval")
    org = _org_for_approval(approval_id) if approval_id else None
    if not org:
        org = settings_ops._resolve_settings_caller(None)
    if not org or org == "personal":
        return PlainTextResponse(
            "this request does not say which organization to sign for: pass "
            "?approval=<id> so the organization can be read from the request "
            "being signed",
            status_code=400,
        )

    row = settings_ops.read_set_key(
        SIGN_KEY_SET_ID, org, org="personal", peers=[],
    )
    armored = (row or {}).get("payload", {}).get("armored_private_key")
    if not armored:
        return PlainTextResponse(
            f"no signing key is configured for {org!r} — add one at "
            f"{SIGN_KEY_SET_ID} key={org!r} in the operator's own store",
            status_code=404,
        )
    return PlainTextResponse(armored)


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
    return {"files": files, "patch": patch,
            "org": _org_for_approval(row.get("id")) }


# Share-link approval kinds (link_publish / link_revoke) live in their own
# module; it exports plain dicts so this file stays the single registry.
from tools.dashboard import link_approvals as _link_approvals
from tools.dashboard import dashboard_access_approvals as _dashboard_access
from tools.dashboard import visitor_approvals as _visitor
from tools.dashboard import mcp_peer_approvals as _mcp_peer
from tools.dashboard import secure_setting_approvals as _secure_setting
from tools.dashboard import fleet_enrollment_approvals as _fleet_enrollment
from tools.dashboard import external_service_approvals as _external_service
from tools.dashboard import vault_open_approvals as _vault_open

# Optional per-kind request preparation. A handler returns the normalized
# request plus a server-frozen staged context. Kinds absent here retain the
# generalized primitive's historical pass-through behavior.
PREPARE_CREATE = {
    "link_publish": _link_approvals.prepare_create,
    **_dashboard_access.PREPARE_CREATE,
    **_visitor.PREPARE_CREATE,
    **_mcp_peer.PREPARE_CREATE,
    **_secure_setting.PREPARE_CREATE,
    **_fleet_enrollment.PREPARE_CREATE,
    **_external_service.PREPARE_CREATE,
}
# Kinds in this registry must derive their requester identity from the
# middleware-established principal.  The historical caller-claimed ``session``
# field is passed only so the handler can explicitly ignore/reject it.
PREPARE_CREATE_FROM_REQUEST = {
    **_vault_open.PREPARE_CREATE_FROM_REQUEST,
}
AUTHORIZE_DECISION = {
    **_dashboard_access.AUTHORIZE_DECISION,
    **_visitor.AUTHORIZE_DECISION,
    **_mcp_peer.AUTHORIZE_DECISION,
    **_secure_setting.AUTHORIZE_DECISION,
    **_fleet_enrollment.AUTHORIZE_DECISION,
    **_external_service.AUTHORIZE_DECISION,
    **_vault_open.AUTHORIZE_DECISION,
}
AUTHORIZE_GET = {
    **_vault_open.AUTHORIZE_GET,
}

# Per-kind GET enrichment — the only kind-specific hook on the server side of
# the primitive. A kind whose stored request is self-describing needs no entry.
ENRICH = {
    "commit_sign": _enrich_commit_sign,
    **_link_approvals.ENRICH,
    **_dashboard_access.ENRICH,
    **_visitor.ENRICH,
    **_mcp_peer.ENRICH,
    **_secure_setting.ENRICH,
    **_fleet_enrollment.ENRICH,
    **_external_service.ENRICH,
}
ENRICH_FROM_REQUEST = {
    **_vault_open.ENRICH_FROM_REQUEST,
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
    **_dashboard_access.EXECUTORS,
    **_visitor.EXECUTORS,
    **_mcp_peer.EXECUTORS,
    **_secure_setting.EXECUTORS,
    **_fleet_enrollment.EXECUTORS,
    **_external_service.EXECUTORS,
    **_vault_open.EXECUTORS,
}
RESULT_BUILDERS: dict = {
    **_vault_open.RESULT_BUILDERS,
}
WAIT_RESULT_BUILDERS: dict = {
    **_vault_open.WAIT_RESULT_BUILDERS,
}
# Per-kind wake: (row-with-committed-result) -> notification spec | None. A kind
# registered here wakes its requesting session by task-notification when the
# decision is committed, so a requester that posted and returned (no held GET)
# learns the outcome without polling.
SESSION_NOTIFIERS: dict = {
    **_vault_open.SESSION_NOTIFIERS,
}


def push_eligible_kind(kind: str) -> bool:
    """Whether this registered approval kind may mint an OS interruption."""
    return (
        kind == "commit_sign"
        or kind in PREPARE_CREATE
        or kind in PREPARE_CREATE_FROM_REQUEST
        or kind in AUTHORIZE_DECISION
        or kind in ENRICH
    )

# Requests whose executor is running: the verdict is committed but the result
# row is written only on completion, so further decisions must be refused here
# rather than by the result-row first-writer-wins check.
_executing: set[str] = set()


def approval_is_executing(request_id: str) -> bool:
    """Read-only lifecycle seam for application projections.

    The generic rendezvous deliberately withholds its result while an
    executor runs.  Consumers may use this bit to distinguish human review
    from post-decision work without copying the decision or inventing their
    own queue.
    """
    return request_id in _executing


def _finalize_decision(rid: str, kind: str, session: str) -> None:
    """Wake held ?wait= calls and tell viewers the request is closed."""
    try:
        web_push.cancel_approval(rid)
    except Exception:
        # Approval truth is already committed and must never be rolled back by
        # a transport cleanup failure. The worker's final source guard still
        # prevents a later semantic decision from being inferred from Push.
        pass
    ev = _decision_waiters.pop(rid, None)
    if ev:
        ev.set()
    event_bus.broadcast_sync("approval:decided",
                             {"id": rid, "kind": kind, "session": session})
    notifier = SESSION_NOTIFIERS.get(kind)
    if notifier and session:
        # Wake the requesting session by task-notification. Best-effort and
        # deduped: a wake failure must never roll back the committed decision,
        # and a held ?wait= GET that already delivered is harmless to duplicate.
        try:
            row = ar.get(rid)
            spec = notifier(row) if row else None
            if spec:
                session_notify.deliver_task_notification_sync(
                    session,
                    spec["notification_id"],
                    kind=spec.get("kind", "system"),
                    status=spec.get("status", "complete"),
                    summary=spec["summary"],
                    body=spec.get("body", ""),
                )
        except Exception:
            pass

# Cap on ?wait= so a stuck client can't hold a connection open indefinitely;
# requesters (e.g. the signing shim) loop on the held GET instead.
MAX_WAIT_S = 60.0


async def open_approval(
    *,
    kind: str,
    session: str,
    request_payload: dict,
    request_id: str | None = None,
    prepared_staged: dict | None = None,
) -> str:
    """Create and announce one prepared approval request.

    Specialized producers use this seam instead of duplicating registry,
    Web Push, and event-bus behavior. A caller may supply a high-entropy id
    when that id is itself its reply capability. ``prepared_staged`` is only
    for a server-owned producer that already resolved a registered capability;
    ordinary callers always pass through the kind's preparation hook.
    """
    staged = prepared_staged
    if (
        isinstance(request_id, str)
        and request_id.startswith(CENTRAL_APPROVAL_ID_PREFIX)
    ):
        raise ValueError("reserved central approval id")
    if staged is None:
        prepare = PREPARE_CREATE.get(kind)
        if prepare:
            request_payload, staged = prepare(session, request_payload)
    if request_id is None:
        rid = ar.create(
            kind=kind, session=session, request=request_payload,
            staged=staged, created_at=time.time(),
        )
    else:
        ar.create_idempotent(
            request_id=request_id,
            kind=kind,
            session=session,
            request=request_payload,
            staged=staged,
            created_at=time.time(),
        )
        rid = request_id
    if push_eligible_kind(kind):
        await web_push.register_approval_pending(rid, kind)
    await event_bus.broadcast(
        "approval:pending", {"id": rid, "kind": kind, "session": session},
    )
    return rid


async def wait_for_approval(rid: str, wait_seconds: float) -> dict | None:
    """Return an approval row, waiting boundedly when it is still pending."""
    row = ar.get(rid)
    if row is None or row["result"] is not None or wait_seconds <= 0:
        return row
    ev = _decision_waiters.setdefault(rid, asyncio.Event())
    # Close the read→waiter registration race: a decision may have committed
    # after the first read but before the event existed, so no finalizer could
    # have signalled it. Re-read once with the event installed.
    latest = ar.get(rid)
    if latest is None or latest["result"] is not None:
        _decision_waiters.pop(rid, None)
        return latest
    try:
        await asyncio.wait_for(ev.wait(), min(wait_seconds, MAX_WAIT_S))
    except asyncio.TimeoutError:
        pass
    return ar.get(rid) or row


async def _create_approval_legacy(request: Request) -> JSONResponse:
    """POST /api/approvals  {kind, session, request} -> {id}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = body.get("kind")
    session = body.get("session")
    req = body.get("request")
    if not isinstance(kind, str) or not kind or len(kind) > 64 \
            or not isinstance(req, dict) or not req:
        return JSONResponse(
            {"error": (
                "kind must be a short non-empty string and request must be a "
                "non-empty object"
            )},
            status_code=400)
    staged = None
    prepare_from_request = PREPARE_CREATE_FROM_REQUEST.get(kind)
    if prepare_from_request:
        try:
            session, req, staged = prepare_from_request(request, session, req)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    else:
        if not isinstance(session, str) or not session or len(session) > 256:
            return JSONResponse(
                {"error": "session must be a short non-empty string"},
                status_code=400,
            )
    try:
        rid = await open_approval(
            kind=kind,
            session=session,
            request_payload=req,
            prepared_staged=staged,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"id": rid})


async def _get_approval_legacy(request: Request) -> JSONResponse:
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
    authorize_get = AUTHORIZE_GET.get(r["kind"])
    if authorize_get:
        error = authorize_get(request, r, wait is not None)
        if error:
            return JSONResponse({"error": error}, status_code=403)
    if wait is not None:
        if r["result"] is None:
            try:
                r = await wait_for_approval(rid, float(wait or 0)) or r
            except ValueError:
                pass
        result = r["result"]
        build_wait_result = WAIT_RESULT_BUILDERS.get(r["kind"])
        if result is not None and build_wait_result:
            result = build_wait_result(r, result)
        return JSONResponse({
            "id": r["id"], "kind": r["kind"], "session": r["session"],
            "request": r["request"], "result": result,
        })
    extra = {}
    enrich_from_request = ENRICH_FROM_REQUEST.get(r["kind"])
    if enrich_from_request:
        try:
            extra = enrich_from_request(request, r) or {}
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except Exception:
            return JSONResponse(
                {"error": "vault factor bootstrap is unavailable"},
                status_code=400,
            )
    else:
        enrich = ENRICH.get(r["kind"])
    if not enrich_from_request and enrich:
        try:
            # Enrichers do blocking store reads (e.g. the mcp-peer link/
            # crosstalk enrichers open org stores via list_orgs + a
            # dashboard.db get_session) — off the loop thread (auto-gwdqq).
            extra = await asyncio.to_thread(enrich, r) or {}
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


async def _decide_approval_legacy(request: Request) -> JSONResponse:
    """POST /api/approvals/{id}/decision  {approved: bool, ...} — the body IS the
    stored result: the operator's true/false plus kind-specific outputs (e.g. the
    armored ``signature``). The request itself is never modified by a decision —
    what was staged is exactly what an approval applies to. First writer wins."""
    rid = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not isinstance(body.get("approved"), bool):
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
    authorize = AUTHORIZE_DECISION.get(r["kind"])
    if authorize:
        error = authorize(request, r, body)
        if error:
            return JSONResponse({"ok": False, "error": error}, status_code=401)

    executor = EXECUTORS.get(r["kind"])
    if body["approved"] and executor:
        _executing.add(rid)
        # Human attention ended when the first decision was accepted, not when
        # potentially long post-approval execution later writes its outcome.
        # Cancel unsent OS delivery at that boundary.
        try:
            web_push.cancel_approval(rid, reason="approval_decision_started")
        except Exception:
            pass

        async def run_and_record():
            try:
                outcome = await executor(r, body)
            except Exception as e:  # the requester gets a failure, never a hang
                outcome = {"ok": False, "error": str(e)}
            finally:
                _executing.discard(rid)
            build_result = RESULT_BUILDERS.get(r["kind"])
            result = (
                build_result(r, body, outcome) if build_result
                else {**body, "execution": outcome}
            )
            if ar.set_result(rid, result):
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


def _central_no_store(payload: dict, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _central_error(exc: ApprovalHttpBridgeError) -> JSONResponse:
    if exc.code in {
        "invalid_request", "unknown_kind", "kind_disabled", "request_conflict",
        "source_expired",
    }:
        status_code = 400
    elif exc.code == "unauthenticated":
        status_code = 401
    elif exc.code == "not_found":
        status_code = 404
    elif exc.code in {"expired", "approval_conflict"}:
        status_code = 409
    elif exc.code == "invalid_decision":
        status_code = 422
    else:
        status_code = 503
    public = (
        "unavailable" if status_code == 503
        else (
            "not found" if exc.code == "not_found"
            else (
                "authentication required"
                if exc.code == "unauthenticated"
                else exc.code
            )
        )
    )
    return _central_no_store({"error": public}, status_code=status_code)


def _approval_http_bridge():
    runtime = attention_routes.approval_runtime()
    assert runtime.approval_http is not None
    return runtime.approval_http


async def create_approval(request: Request) -> JSONResponse:
    """Dispatch an exactly migrated kind to Settings; otherwise stay legacy."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = body.get("kind") if isinstance(body, dict) else None
    bridge = _approval_http_bridge()
    if not bridge.claims_kind(kind):
        return await _create_approval_legacy(request)
    if (
        set(body) != {"kind", "request"}
        or not isinstance(kind, str)
        or not isinstance(body.get("request"), dict)
        or not body["request"]
    ):
        return _central_no_store({"error": "invalid_request"}, status_code=400)
    try:
        approval_id = await asyncio.to_thread(
            bridge.create,
            kind,
            api_auth.principal_from_request(request),
            body["request"],
        )
    except ApprovalHttpBridgeError as exc:
        return _central_error(exc)
    return _central_no_store({"id": approval_id})


async def get_approval(request: Request) -> JSONResponse:
    approval_id = request.path_params["id"]
    if not has_central_approval_prefix(approval_id):
        return await _get_approval_legacy(request)
    principal = api_auth.principal_from_request(request)
    try:
        if "wait" in request.query_params:
            raw_wait = request.query_params.get("wait")
            try:
                wait_seconds = float(raw_wait)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ApprovalHttpBridgeError("invalid_request") from exc
            envelope = await _approval_http_bridge().held_envelope(
                approval_id, principal, wait_seconds,
            )
        else:
            envelope = await asyncio.to_thread(
                _approval_http_bridge().envelope, approval_id, principal,
            )
    except ApprovalHttpBridgeError as exc:
        return _central_error(exc)
    return _central_no_store(envelope)


async def decide_approval(request: Request) -> JSONResponse:
    approval_id = request.path_params["id"]
    if not has_central_approval_prefix(approval_id):
        return await _decide_approval_legacy(request)
    denied = attention_routes.operator_mutation_guard(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not isinstance(body.get("approved"), bool):
        return _central_no_store({"error": "invalid_decision"}, status_code=422)
    try:
        actor = resolve_human_approval_actor(request)
        await asyncio.to_thread(
            _approval_http_bridge().decide_legacy,
            approval_id,
            actor,
            body,
        )
    except ApprovalServiceError as exc:
        code = (
            "unavailable"
            if exc.code == "not_configured"
            else "authentication required"
        )
        status_code = 503 if code == "unavailable" else 401
        return _central_no_store({"error": code}, status_code=status_code)
    except ApprovalHttpBridgeError as exc:
        if exc.code == "approval_conflict":
            return _central_no_store(
                {
                    "ok": False,
                    "error": "approval has a different terminal outcome",
                },
                status_code=409,
            )
        return _central_error(exc)
    return _central_no_store({"ok": True})


async def cancel_approval(request: Request) -> JSONResponse:
    approval_id = request.path_params["id"]
    if not has_central_approval_prefix(approval_id):
        return _central_no_store({"error": "not found"}, status_code=404)
    try:
        raw = await request.body()
        body = {} if not raw else await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or body:
        return _central_no_store({"error": "invalid_request"}, status_code=400)
    try:
        envelope = await asyncio.to_thread(
            _approval_http_bridge().cancel,
            approval_id,
            api_auth.principal_from_request(request),
        )
    except ApprovalHttpBridgeError as exc:
        return _central_error(exc)
    return _central_no_store(envelope)


ROUTES = [
    Route("/api/approvals", create_approval, methods=["POST"]),
    Route("/api/approvals/{id}", get_approval, methods=["GET"]),
    Route("/api/approvals/{id}/decision", decide_approval, methods=["POST"]),
    Route("/api/approvals/{id}/cancel", cancel_approval, methods=["POST"]),
    Route("/api/sign-key", get_sign_key, methods=["GET"]),
]
