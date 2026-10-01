"""Authenticated HTTP and private invalidation surface for Central Attention.

Durable truth remains in the Settings-backed services.  This module exposes
only safe projections and a bounded wake/refetch stream; it owns no queue and
never executes an approved application operation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import threading
from typing import Any, Mapping
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from tools.dashboard import api_auth, dashboard_access_central, unlock_routes
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    ApprovalStatus,
    HumanApprovalActor,
    resolve_human_approval_actor,
)
from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridge,
    ApprovalHttpRegistry,
    ApprovalWaitHub,
)
from tools.dashboard.attention_registry import APPLICATIONS
from tools.graph.schemas.central_attention import (
    APPROVAL_REQUEST_SET_ID,
    APPROVAL_RESOLUTION_SET_ID,
)


logger = logging.getLogger(__name__)

PRIVATE_CENTRAL_SET_IDS = frozenset({
    APPROVAL_REQUEST_SET_ID,
    APPROVAL_RESOLUTION_SET_ID,
})
_APPROVAL_SET_IDS = frozenset({APPROVAL_REQUEST_SET_ID, APPROVAL_RESOLUTION_SET_ID})
_SSE_CLOSE = object()
_SUBSCRIBER_QUEUE_SIZE = 32
_HEARTBEAT_SECONDS = 15.0
_MAX_MUTATION_BODY_BYTES = 32 * 1024
_DECIDED_SHOWN_SECONDS = 7 * 86400


def is_private_central_set_id(value: Any) -> bool:
    return isinstance(value, str) and value in PRIVATE_CENTRAL_SET_IDS


class PrivateAttentionHub:
    """Tells every open inbox page to refetch when an approval row changes."""

    def __init__(self, *, subscriber_queue_size: int = _SUBSCRIBER_QUEUE_SIZE) -> None:
        if subscriber_queue_size < 1:
            raise ValueError("attention hub bounds must be positive")
        self._subscriber_queue_size = subscriber_queue_size
        self._thread_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        with self._thread_lock:
            if self._loop is not None and self._loop is not loop:
                raise RuntimeError("attention hub already belongs to another loop")
            self._loop = loop

    async def stop(self) -> None:
        with self._thread_lock:
            self._loop = None
        for queue in tuple(self._subscribers):
            self._close_queue(queue)
        self._subscribers.clear()

    def emit_setting_change(self, *, operation: Any, snapshot: Any, org: Any) -> None:
        """A committed Settings write, from any thread."""
        if org is None and isinstance(snapshot, Mapping) and snapshot.get("set_id") in _APPROVAL_SET_IDS:
            self.emit_refresh()

    def emit_refresh(self) -> None:
        """Ask every open page to refetch, from any thread."""
        with self._thread_lock:
            loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, "attention:refresh", {})
        except RuntimeError:
            return

    def subscribe(self) -> asyncio.Queue:
        if self._loop is not asyncio.get_running_loop():
            raise RuntimeError("attention hub is not running on this loop")
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._subscriber_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @staticmethod
    def _close_queue(queue: asyncio.Queue) -> None:
        try:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(_SSE_CLOSE)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass

    def _fanout(self, event: str, data: Mapping[str, Any]) -> None:
        frame = (event, dict(data))
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)
                self._close_queue(queue)


@dataclass(slots=True)
class AttentionRouteRuntime:
    approvals: ApprovalService
    hub: PrivateAttentionHub
    inbox_texts: Mapping[str, Any]
    approval_http: ApprovalHttpBridge | None = None
    approval_reconciler: Any | None = None
    operator_result_projectors: Mapping[str, Any] | None = None
    vault_open_delivery: Any | None = None
    link_operation_desk: Any | None = None
    enrollment_desk: Any | None = None
    crosstalk_desk: Any | None = None
    jira_write_desk: Any | None = None

    def __post_init__(self) -> None:
        if self.operator_result_projectors is None:
            self.operator_result_projectors = {}
        elif not isinstance(self.operator_result_projectors, Mapping) or any(
            not isinstance(kind, str) or not callable(projector)
            for kind, projector in self.operator_result_projectors.items()
        ):
            raise ValueError("operator result projectors are invalid")
        else:
            self.operator_result_projectors = dict(self.operator_result_projectors)
        if self.approval_http is None:
            self.approval_http = ApprovalHttpBridge(
                approvals=self.approvals,
                registry=ApprovalHttpRegistry(approvals=self.approvals.registry),
            )
        elif (
            self.approval_http.approvals is not self.approvals
            or self.approval_http.registry.approvals is not self.approvals.registry
        ):
            raise ValueError("Central route runtime must share one exact composition")


def session_requester_label(subject: str) -> str | None:
    """The human-readable name of the SESSION asking for an approval:
    its tmux name plus the working title it set with ``graph set-label``,
    e.g. ``auto-0910-155648 · auto-nh1po: machine-targeted serve routing``.

    This is what every approval renderer shows as "Requested by". Without
    it the requester was ``{kind: session}`` and nothing else, and the
    operator was asked to grant Dashboard access to "Authenticated
    session" with no way to tell which of a dozen live sessions wanted it.
    The label is frozen on the request at creation, so a later rename
    does not rewrite history. Never raises: an unreadable dashboard DB
    yields the bare tmux name."""
    if not isinstance(subject, str) or not subject:
        return None
    try:
        from tools.dashboard.dao import dashboard_db
        row = dashboard_db.get_session(subject)
    except Exception:
        row = None
    title = row.get("label") if isinstance(row, dict) else None
    if isinstance(title, str) and title.strip():
        return f"{subject} · {title.strip()}"
    return subject


def session_requester_view(requester: Mapping[str, Any]) -> dict[str, str]:
    """Resolve a viewer destination only after matching the canonical identity.

    Existing frozen labels contain the session handle. It is a lookup hint,
    never identity evidence: recompute the scope-bound ID before linking it.
    No grant/signature field is changed and no raw subject is persisted.
    """
    if requester.get("kind") != "session":
        return {}
    from urllib.parse import quote
    from tools.dashboard.approval_service import canonical_session_requester_id
    from tools.dashboard.dao import dashboard_db
    from tools.dashboard.org_identity import resolve_session_org

    label = requester.get("label")
    if not isinstance(label, str):
        return {}
    subject = label.split(" · ", 1)[0]
    row = dashboard_db.get_session(subject)
    if not row or not row.get("project") or row.get("tmux_name") != subject:
        return {}
    org = resolve_session_org(row).get("slug")
    candidates = [api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject=subject)]
    if org:
        candidates.append(api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.ORG_SESSION, subject=subject, org=org))
    if not any(canonical_session_requester_id(principal) == requester.get("id") for principal in candidates):
        return {}
    return {
        "href": f"/session/{quote(row['project'], safe='')}/{quote(subject, safe='')}",
        "byline": row["project"],
    }


def pending_session_approval(session: Mapping[str, Any], actor: HumanApprovalActor) -> dict | None:
    """Project an existing Central request into the session viewer's opener."""
    from tools.dashboard.approval_service import canonical_session_requester_id
    from tools.dashboard.org_identity import resolve_session_org

    subject = session["tmux_name"]
    org = resolve_session_org(session).get("slug")
    principals = [api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject=subject)]
    if org:
        principals.append(api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.ORG_SESSION, subject=subject, org=org))
    identities = {canonical_session_requester_id(p) for p in principals}
    found = []
    for item in _inbox_items():
        status = item.status
        payload = status.request.payload
        requester = payload.get("requester_ref", {})
        if (status.resolution is None and requester.get("kind") == "session"
                and requester.get("id") in identities
                and payload.get("decider") == {"kind": "person", "id": actor.decider_ref}):
            approval_id = status.request.approval_id
            found.append((item.occurred_at, approval_id,
                          {"id": approval_id, "kind": payload["kind"], "attention_id": approval_id}))
    return min(found, key=lambda row: row[:2])[2] if found else None


def build_production_runtime() -> AttentionRouteRuntime:
    from tools.dashboard import fleet_enrollment_approvals as fleet
    from tools.dashboard import mailbox_central
    from tools.dashboard import vault_open_central
    from tools.dashboard import link_approval_central
    from tools.dashboard import external_service_approvals as external
    from tools.dashboard import mcp_crosstalk_central as crosstalk
    from tools.dashboard import visitor_approvals as visitor
    from tools.dashboard import jira_central
    from tools.dashboard import vault_seal_central as vault_seal
    dashboard_approval_runtime = dashboard_access_central.build_approval_runtime()
    approval_registry = build_production_registry(runtimes={
        dashboard_access_central.KIND: dashboard_approval_runtime,
        fleet.KIND: fleet.build_approval_runtime(),
        mailbox_central.KIND: mailbox_central.build_approval_runtime(),
        vault_open_central.KIND: vault_open_central.build_approval_runtime(),
        external.KIND: external.build_approval_runtime(),
        crosstalk.KIND: crosstalk.build_approval_runtime(),
        visitor.KIND: visitor.build_approval_runtime(),
        jira_central.KIND: jira_central.build_approval_runtime(),
        vault_seal.KIND: vault_seal.build_approval_runtime(),
        **{kind: link_approval_central.build_approval_runtime(kind) for kind in link_approval_central.KINDS},
    })
    approval_waiters = ApprovalWaitHub()

    def approval_after_commit(record_type: str, approval_id: str) -> None:
        # The kind handlers hear this same commit as a Settings change event
        # (emit_setting_change); only waiters and Fleet are woken here.
        approval_waiters.notify(record_type, approval_id)
        record = approvals.store.get_request(approval_id)
        if record is not None and record.payload.get("kind") == fleet.KIND:
            fleet.reconcile(approval_id)

    approvals = ApprovalService(
        registry=approval_registry,
        after_commit=approval_after_commit,
        session_label_resolver=session_requester_label,
        registered_service_label_resolver=crosstalk.service_label,
    )
    consumer = dashboard_access_central.DashboardAccessResultConsumer()
    coordinator = dashboard_access_central.DashboardAccessCoordinator(
        approvals=approvals, consumer=consumer,
    )
    email_consumer = mailbox_central.EmailSendConsumer()
    email_coordinator = mailbox_central.EmailSendCoordinator(
        approvals=approvals, consumer=email_consumer,
    )
    vault_delivery = vault_open_central.VaultOpenDelivery(approvals=approvals)
    vault_coordinator = vault_open_central.VaultOpenCoordinator(
        delivery=vault_delivery, approvals=approvals,
    )
    enrollment_desk = external.EnrollmentDesk(approvals=approvals)
    crosstalk_desk = crosstalk.CrosstalkDesk(approvals=approvals)
    crosstalk_coordinator = crosstalk.CrosstalkCoordinator(desk=crosstalk_desk, approvals=approvals)
    visitor_desk = visitor.VisitorDesk(approvals=approvals)
    jira_desk = jira_central.JiraWriteDesk(approvals=approvals)
    jira_coordinator = jira_central.JiraWriteCoordinator(desk=jira_desk, approvals=approvals)
    link_desk = link_approval_central.LinkApprovalDesk(approvals=approvals)
    vault_seal_coordinator = vault_seal.VaultSealCoordinator(approvals=approvals)
    # The kinds that act when a decision arrives; each ignores other kinds' ids.
    reconcilers = mailbox_central.ReconcilerGroup(
        coordinator, email_coordinator, vault_coordinator, crosstalk_coordinator, jira_coordinator,
        vault_seal_coordinator,
    )
    approval_http = ApprovalHttpBridge(
        approvals=approvals,
        registry=ApprovalHttpRegistry(
            approvals=approval_registry,
            adapters={
                dashboard_access_central.KIND: dashboard_access_central.build_http_adapter(
                    consumer, reconcile=coordinator.reconcile_exact,
                ),
                mailbox_central.KIND: mailbox_central.build_http_adapter(
                    email_consumer, reconcile=email_coordinator.reconcile_exact,
                ),
                jira_central.KIND: jira_central.build_http_adapter(
                    jira_desk, reconcile=jira_coordinator.reconcile_exact,
                ),
                visitor.KIND: visitor.build_http_adapter(visitor_desk),
                vault_seal.KIND: vault_seal.build_http_adapter(),
                vault_open_central.KIND: vault_open_central.build_http_adapter(
                    vault_delivery, reconcile=vault_coordinator.reconcile_exact,
                ),
                **{kind: link_approval_central.build_http_adapter(kind, link_desk)
                   for kind in link_approval_central.KINDS},
            },
        ),
        wait_hub=approval_waiters,
    )
    return AttentionRouteRuntime(
        approvals=approvals,
        hub=PrivateAttentionHub(),
        inbox_texts={
            dashboard_access_central.KIND: dashboard_access_central.inbox_text,
            fleet.KIND: fleet.inbox_text,
            mailbox_central.KIND: mailbox_central.inbox_text,
            vault_open_central.KIND: vault_open_central.inbox_text,
            external.KIND: external.inbox_text,
            crosstalk.KIND: crosstalk.inbox_text,
            visitor.KIND: visitor.inbox_text,
            jira_central.KIND: jira_central.inbox_text,
            vault_seal.KIND: vault_seal.inbox_text,
            **{kind: link_approval_central.inbox_text for kind in link_approval_central.KINDS},
        },
        approval_http=approval_http,
        approval_reconciler=reconcilers,
        operator_result_projectors={dashboard_access_central.KIND: consumer.project,
                                    fleet.KIND: fleet.project_result,
                                    mailbox_central.KIND: email_consumer.project,
                                    vault_open_central.KIND: vault_delivery.operator_result,
                                    external.KIND: enrollment_desk.operator_result,
                                    crosstalk.KIND: crosstalk_desk.operator_result,
                                    visitor.KIND: visitor_desk.operator_result,
                                    jira_central.KIND: jira_desk.operator_result,
                                    vault_seal.KIND: vault_seal.project_result,
                                    **{kind: link_desk.operator_result
                                       for kind in link_approval_central.KINDS}},
        vault_open_delivery=vault_delivery,
        link_operation_desk=link_desk,
        enrollment_desk=enrollment_desk,
        crosstalk_desk=crosstalk_desk,
        jira_write_desk=jira_desk,
    )


_runtime = build_production_runtime()


def configure_runtime(runtime: AttentionRouteRuntime) -> AttentionRouteRuntime:
    """Replace composition for hermetic tests or later migrated-kind wiring."""
    global _runtime
    if not isinstance(runtime, AttentionRouteRuntime):
        raise ValueError("invalid Central Attention route runtime")
    previous = _runtime
    _runtime = runtime
    return previous


async def start() -> None:
    await _runtime.hub.start()
    if _runtime.approval_reconciler is not None:
        await _runtime.approval_reconciler.start()


async def stop() -> None:
    if _runtime.approval_reconciler is not None:
        await _runtime.approval_reconciler.stop()
    await _runtime.hub.stop()
    assert _runtime.approval_http is not None
    _runtime.approval_http.close()


def approval_runtime() -> AttentionRouteRuntime:
    """Return the one process-owned Central composition to route adapters."""
    return _runtime


def _push_for_approval_row(set_id: Any, approval_id: Any) -> None:
    """A new request pushes to the devices subscribed on this machine, and a
    decision cancels any push still waiting. Every machine the row reaches
    pushes to its own subscribers; a phone shows one notification per
    approval, because the push tag is derived from the approval."""
    from tools.dashboard import web_push
    if not isinstance(approval_id, str) or not approval_id:
        return
    if set_id == APPROVAL_RESOLUTION_SET_ID:
        web_push.cancel_approval(approval_id)
        return
    if set_id != APPROVAL_REQUEST_SET_ID:
        return
    item = _exact_item(approval_id)
    if item is not None and item.status.resolution is None:
        web_push.register_approval_pending_sync(approval_id, item.registration.kind)


def emit_setting_change(*, operation: str, snapshot: Mapping[str, Any], org: str | None) -> None:
    try:
        if org is None and isinstance(snapshot, Mapping):
            _push_for_approval_row(snapshot.get("set_id"), snapshot.get("key"))
        if _runtime.approval_reconciler is not None:
            _runtime.approval_reconciler.offer_local_setting(
                operation=operation,
                snapshot=snapshot,
                org=org,
            )
        _runtime.hub.emit_setting_change(
            operation=operation, snapshot=snapshot, org=org,
        )
    except Exception:
        logger.warning("private attention post-commit hint failed", exc_info=True)


def emit_personal_sync_change(*, addresses=()) -> None:
    """Accept one payload-free post-materialization hint from Fleet sync."""
    try:
        addresses = tuple(addresses or ())
        if _runtime.approval_reconciler is not None:
            _runtime.approval_reconciler.offer_synced(addresses=addresses)
        if any(getattr(a, "set_id", None) in _APPROVAL_SET_IDS for a in addresses):
            _runtime.hub.emit_refresh()
        for address in addresses:
            _push_for_approval_row(getattr(address, "set_id", None), getattr(address, "key", None))
    except Exception:
        logger.warning("personal-sync approval hint failed", exc_info=True)


def _no_store(payload: Mapping[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        dict(payload), status_code=status_code, headers={"Cache-Control": "no-store"},
    )


def _operator_guard(request: Request) -> JSONResponse | None:
    principal = api_auth.principal_from_request(request)
    if principal.kind is api_auth.ApiPrincipalKind.OPERATOR_COOKIE:
        return None
    if (
        principal.kind is api_auth.ApiPrincipalKind.COMPATIBILITY
        and not unlock_routes.gate_enforced()
    ):
        return None
    if principal.kind is api_auth.ApiPrincipalKind.COMPATIBILITY:
        return _no_store({"error": "authentication required"}, status_code=401)
    return _no_store({"error": "operator authority required"}, status_code=403)


def _same_origin_guard(request: Request) -> JSONResponse | None:
    principal = api_auth.principal_from_request(request)
    if (
        principal.kind is api_auth.ApiPrincipalKind.COMPATIBILITY
        and not unlock_routes.gate_enforced()
    ):
        return None
    origin = request.headers.get("origin")
    if not origin:
        return _no_store({"error": "same-origin request required"}, status_code=403)
    try:
        supplied = urlsplit(origin)
        requested = request.url
        if (
            supplied.scheme != "https"
            or requested.scheme != "https"
            or supplied.username is not None
            or supplied.password is not None
            or supplied.query
            or supplied.fragment
            or supplied.path
            or not supplied.hostname
        ):
            raise ValueError
        supplied_port = supplied.port or 443
        requested_port = requested.port or 443
        if (
            supplied.hostname.lower() != (requested.hostname or "").lower()
            or supplied_port != requested_port
        ):
            raise ValueError
    except Exception:
        return _no_store({"error": "same-origin request required"}, status_code=403)
    return None


def operator_mutation_guard(request: Request) -> JSONResponse | None:
    """Shared human-operator and exact same-origin mutation boundary."""
    return _operator_guard(request) or _same_origin_guard(request)


async def _strict_json_object(request: Request, *, allow_empty: bool = False) -> dict[str, Any]:
    collected = bytearray()
    async for chunk in request.stream():
        if len(collected) + len(chunk) > _MAX_MUTATION_BODY_BYTES:
            raise ValueError("body is too large")
        collected.extend(chunk)
    raw = bytes(collected)
    if not raw and allow_empty:
        return {}

    def closed_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=closed_object,
            parse_constant=reject_constant,
        )
    except Exception as exc:
        raise ValueError("body must be JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("body must be a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class InboxItem:
    """One approval as the inbox shows it, read from its request and decision."""

    status: ApprovalStatus
    registration: Any
    title: str
    summary: str | None

    @property
    def approval_id(self) -> str:
        return self.status.request.approval_id

    @property
    def occurred_at(self) -> float:
        resolution = self.status.resolution
        if resolution is not None:
            return float(resolution.payload["resolved_at"])
        return float(self.status.request.payload["created_at"])


def _inbox_item(status: ApprovalStatus) -> InboxItem | None:
    kind = status.request.payload.get("kind")
    text = _runtime.inbox_texts.get(kind)
    registration = _runtime.approvals.registry.kinds.get(kind)
    if text is None or registration is None or registration.runtime is None:
        return None
    title, summary = text(status)
    return InboxItem(status, registration, title, summary)


def _inbox_items() -> list[InboxItem]:
    """Every open approval, and those decided in the last week."""
    since = _runtime.approvals.now() - _DECIDED_SHOWN_SECONDS
    items = [
        item for item in map(_inbox_item, _runtime.approvals.list_statuses())
        if item and (item.status.resolution is None or item.occurred_at >= since)
    ]
    items.sort(key=lambda item: (-item.occurred_at, item.approval_id))
    return items


def _exact_item(approval_id: str) -> InboxItem | None:
    try:
        status = _runtime.approvals.status(approval_id)
    except ApprovalServiceError as exc:
        if exc.code == "not_found":
            return None
        raise
    return _inbox_item(status)


def _safe_item(item: InboxItem) -> dict[str, Any]:
    scope = item.status.request.payload["application_scope"]
    label, icon_ref = APPLICATIONS.get(scope, (scope, ""))
    return {
        "attention_id": item.approval_id,
        "application": {"scope": scope, "label": label, "icon_ref": icon_ref},
        "category": "approvals",
        "participant_role": "recipient",
        "attention_state": "resolved" if item.status.resolution is not None else "needs_attention",
        "title": item.title,
        "summary": item.summary,
        "counterparty_ref": None,
        "occurred_at": item.occurred_at,
        "open": {"mode": "registered_renderer", "renderer_id": item.registration.renderer_id},
    }


def _safe_resolution(record: Any) -> dict[str, Any] | None:
    if record is None:
        return None
    payload = record.payload
    return {"outcome": payload["outcome"], "resolved_at": payload["resolved_at"]}


async def api_attention_items(request: Request):
    denied = _operator_guard(request)
    if denied is not None:
        return denied
    if request.query_params:
        return _no_store({"error": "invalid_request"}, status_code=400)
    try:
        items = await asyncio.to_thread(_inbox_items)
    except ApprovalServiceError:
        return _no_store({"error": "unavailable"}, status_code=503)
    applications: dict[str, dict[str, int]] = {}
    waiting = 0
    for item in items:
        scope = item.status.request.payload["application_scope"]
        counts = applications.setdefault(scope, {"needs_attention": 0, "waiting": 0})
        if item.status.resolution is None:
            counts["needs_attention"] += 1
            waiting += 1
    return _no_store({
        "items": [_safe_item(item) for item in items],
        "counts": {
            "total_needs_attention": waiting,
            "categories": {"apps": 0, "comms": 0, "approvals": waiting},
            "applications": applications,
        },
    })


async def api_attention_item(request: Request):
    denied = _operator_guard(request)
    if denied is not None:
        return denied
    try:
        item = await asyncio.to_thread(_exact_item, request.path_params["attention_id"])
    except ApprovalServiceError:
        return _no_store({"error": "unavailable"}, status_code=503)
    if item is None:
        return _no_store({"error": "not_found"}, status_code=404)
    status = item.status
    request_payload = status.request.payload
    actions: list[str] = []
    if status.resolution is None:
        try:
            actor = resolve_human_approval_actor(request)
            if request_payload.get("decider") == {"kind": "person", "id": actor.decider_ref}:
                actions = ["granted", "declined"]
        except ApprovalServiceError:
            pass
    requester = request_payload["requester_ref"]
    safe_requester = {"kind": requester["kind"]}
    if requester.get("label"):
        safe_requester["label"] = requester["label"]
    try:
        safe_requester.update(await asyncio.to_thread(session_requester_view, requester))
    except Exception:
        pass  # Unavailable metadata must never invent a destination.
    review = {
        "type": "approval",
        "renderer_id": item.registration.renderer_id,
        "kind": item.registration.kind,
        "authority_requirement": item.registration.authority_requirement.value,
        "safe_review": dict(request_payload["safe_review"]),
        "requester": safe_requester,
        "resolution": _safe_resolution(status.resolution),
        "actions": actions,
    }
    assert _runtime.operator_result_projectors is not None
    projector = _runtime.operator_result_projectors.get(item.registration.kind)
    if projector is not None:
        try:
            projected = await asyncio.to_thread(projector, status)
            review["application_result"] = None if projected is None else (
                ApprovalHttpBridge._json_mapping(projected, code="unavailable"))
        except Exception:
            return _no_store({"error": "unavailable"}, status_code=503)
    return _no_store({"item": _safe_item(item), "review": review})


async def api_attention_decision(request: Request):
    denied = operator_mutation_guard(request)
    if denied is not None:
        return denied
    try:
        body = await _strict_json_object(request)
    except ValueError:
        return _no_store({"error": "invalid_request"}, status_code=422)
    if set(body) != {"outcome", "decision"} or body.get("outcome") not in {
        "granted", "declined",
    } or not isinstance(body.get("decision"), dict):
        return _no_store({"error": "invalid_decision"}, status_code=422)
    try:
        item = await asyncio.to_thread(_exact_item, request.path_params["attention_id"])
    except ApprovalServiceError:
        return _no_store({"error": "unavailable"}, status_code=503)
    if item is None:
        return _no_store({"error": "not_found"}, status_code=404)
    try:
        actor = resolve_human_approval_actor(request)
    except ApprovalServiceError as exc:
        status_code = 503 if exc.code == "not_configured" else 401
        return _no_store({"error": exc.code}, status_code=status_code)
    try:
        resolution = await asyncio.to_thread(
            _runtime.approvals.decide,
            item.approval_id,
            actor,
            outcome=body["outcome"],
            decision=body["decision"],
        )
    except ApprovalServiceError as exc:
        public = _safe_resolution(exc.resolution)
        if exc.code == "expired" and public is not None:
            return _no_store({"resolution": public}, status_code=409)
        if exc.code in {"unauthenticated"}:
            return _no_store({"error": exc.code}, status_code=401)
        if exc.code in {"wrong_decider", "not_found"}:
            return _no_store({"error": "not_found"}, status_code=404)
        if exc.code in {"invalid_decision", "invalid_request"}:
            return _no_store({"error": exc.code}, status_code=422)
        return _no_store({"error": "unavailable"}, status_code=503)
    public = _safe_resolution(resolution)
    if public is not None and public["outcome"] == "expired":
        return _no_store({"resolution": public}, status_code=409)
    return _no_store({"resolution": public})


_VAULT_OPEN_STATUS = {
    "invalid_request": 422, "not_found": 404, "not_actionable": 409,
    "session_gone": 409, "elsewhere": 409, "binding_drift": 409,
    "open_failed": 409, "delivery_failed": 503, "unavailable": 503,
}


def _vault_open_refusal(exc: Exception) -> JSONResponse:
    # A fixed code only: the request body (a content key) is never echoed.
    code = getattr(exc, "code", "unavailable")
    if code not in _VAULT_OPEN_STATUS:
        code = "unavailable"
    return _no_store({"error": code}, status_code=_VAULT_OPEN_STATUS[code])


async def api_attention_jira_write_content(request: Request):
    """The whole staged content of one jira_write item, for its review
    (operator only, on the accepting machine, sha256-verified). Text is JSON
    lines; an attachment is inline only as a magic-checked raster image,
    otherwise a download. See jira_central.JiraWriteDesk.content."""
    denied = _operator_guard(request)
    if denied is not None:
        return denied
    desk = _runtime.jira_write_desk
    if desk is None:
        return _no_store({"error": "not_found"}, status_code=404)
    try:
        form, body, headers = await asyncio.to_thread(
            desk.content, request.path_params["attention_id"],
        )
    except Exception as exc:
        code = getattr(exc, "code", "unavailable")
        status_code = {"not_found": 404, "elsewhere": 409, "content_missing": 410,
                       "content_mismatch": 409}.get(code, 503)
        return _no_store({"error": code if status_code != 503 else "unavailable"},
                         status_code=status_code)
    if form == "json":
        return JSONResponse(body, headers=headers)
    return Response(body, headers=headers)


async def api_attention_vault_open_bootstrap(request: Request):
    """The factor ceremony and open bundle for one vault_open item (operator
    only, on the accepting machine, while it is pending or granted and not
    yet delivered)."""
    denied = _operator_guard(request)
    if denied is not None:
        return denied
    delivery = _runtime.vault_open_delivery
    if delivery is None:
        return _no_store({"error": "not_found"}, status_code=404)
    try:
        result = await asyncio.to_thread(
            delivery.bootstrap, request.path_params["attention_id"],
        )
    except Exception as exc:
        return _vault_open_refusal(exc)
    return _no_store(dict(result))


async def api_attention_vault_open_delivery(request: Request):
    """Deliver a granted vault_open release with the operator's content key.

    The body is exactly ``{"content_key": <64 hex>}``. It is never logged,
    echoed or stored; see vault_open_central.VaultOpenDelivery.deliver.
    """
    denied = operator_mutation_guard(request)
    if denied is not None:
        return denied
    delivery = _runtime.vault_open_delivery
    if delivery is None:
        return _no_store({"error": "not_found"}, status_code=404)
    try:
        body = await _strict_json_object(request)
    except ValueError:
        return _no_store({"error": "invalid_request"}, status_code=422)
    try:
        result = await asyncio.to_thread(
            delivery.deliver, request.path_params["attention_id"], body,
        )
    except Exception as exc:
        return _vault_open_refusal(exc)
    finally:
        if isinstance(body, dict):
            body.clear()
    return _no_store(dict(result))


_LINK_OPERATION_STATUS = {
    "invalid_request": 422, "not_found": 404, "not_actionable": 409,
    "elsewhere": 409, "stale_envelope": 409,
    "authority_refused": 409, "running": 409, "unavailable": 503,
}


def _link_operation_refusal(exc: Exception) -> JSONResponse:
    # A fixed code, plus the verifier's own words for authority_refused. The
    # signed envelope is never echoed.
    code = getattr(exc, "code", "unavailable")
    if code not in _LINK_OPERATION_STATUS:
        code = "unavailable"
    body = {"error": code}
    detail = getattr(exc, "detail", None)
    if code in ("authority_refused", "invalid_request") and isinstance(detail, str):
        body["detail"] = detail[:500]
    return _no_store(body, status_code=_LINK_OPERATION_STATUS[code])


async def api_attention_link_operation_bootstrap(request: Request):
    """What the operator signs for one link item: the frozen registry request
    (operator only, on the accepting machine, pending or awaiting operation)."""
    denied = _operator_guard(request)
    if denied is not None:
        return denied
    desk = _runtime.link_operation_desk
    if desk is None:
        return _no_store({"error": "not_found"}, status_code=404)
    try:
        result = await asyncio.to_thread(desk.bootstrap, request.path_params["attention_id"])
    except Exception as exc:
        return _link_operation_refusal(exc)
    return _no_store(dict(result))


async def api_attention_link_operation(request: Request):
    """Carry out a granted link approval with the operator's signed envelope:
    exactly ``{"envelope": ...}`` or ``{"envelope": ..., "ttl": ...}``."""
    denied = operator_mutation_guard(request)
    if denied is not None:
        return denied
    desk = _runtime.link_operation_desk
    if desk is None:
        return _no_store({"error": "not_found"}, status_code=404)
    try:
        body = await _strict_json_object(request)
    except ValueError:
        return _no_store({"error": "invalid_request"}, status_code=422)
    try:
        result = await desk.operate(request.path_params["attention_id"], body)
    except Exception as exc:
        return _link_operation_refusal(exc)
    finally:
        body.clear()
    return _no_store(dict(result))


def _sse_frame(event: str, data: Mapping[str, Any]) -> bytes:
    payload = json.dumps(dict(data), sort_keys=True, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


async def api_attention_events(request: Request):
    denied = _operator_guard(request)
    if denied is not None:
        return denied
    try:
        queue = _runtime.hub.subscribe()
    except RuntimeError:
        return _no_store({"error": "unavailable"}, status_code=503)

    async def stream():
        try:
            yield _sse_frame("attention:ready", {})
            while True:
                try:
                    frame = await asyncio.wait_for(
                        queue.get(), timeout=_HEARTBEAT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                if frame is _SSE_CLOSE:
                    return
                event, data = frame
                yield _sse_frame(event, data)
        finally:
            _runtime.hub.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


routes = [
    Route("/api/attention/items", api_attention_items, methods=["GET"]),
    Route("/api/attention/events", api_attention_events, methods=["GET"]),
    Route(
        "/api/attention/items/{attention_id:path}/approval-decision",
        api_attention_decision,
        methods=["POST"],
    ),
    Route(
        "/api/attention/items/{attention_id:path}/jira-write-content",
        api_attention_jira_write_content,
        methods=["GET"],
    ),
    Route(
        "/api/attention/items/{attention_id:path}/vault-open-bootstrap",
        api_attention_vault_open_bootstrap,
        methods=["GET"],
    ),
    Route(
        "/api/attention/items/{attention_id:path}/vault-open-delivery",
        api_attention_vault_open_delivery,
        methods=["POST"],
    ),
    Route(
        "/api/attention/items/{attention_id:path}/link-operation-bootstrap",
        api_attention_link_operation_bootstrap,
        methods=["GET"],
    ),
    Route(
        "/api/attention/items/{attention_id:path}/link-operation",
        api_attention_link_operation,
        methods=["POST"],
    ),
    # The approved opaque-ID rule permits slash characters. Action routes must
    # precede this greedy exact-item route so every valid encoded ID remains
    # addressable without shadowing its nested command path.
    Route(
        "/api/attention/items/{attention_id:path}",
        api_attention_item,
        methods=["GET"],
    ),
]
