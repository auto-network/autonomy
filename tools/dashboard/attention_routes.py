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
import unicodedata
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
    resolve_human_approval_actor,
)
from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridge,
    ApprovalHttpRegistry,
    ApprovalWaitHub,
)
from tools.dashboard.attention_index_service import (
    AttentionIndexError,
    AttentionIndexService,
    AttentionQueryItem,
    AttentionQueryResult,
    canonical_attention_event_id,
)
from tools.dashboard.attention_presentation_service import (
    AttentionPresentationError,
    AttentionPresentationService,
)
from tools.dashboard.attention_registry import build_production_attention_registry
from tools.graph.schemas.central_attention import (
    APPROVAL_REQUEST_SET_ID,
    APPROVAL_RESOLUTION_SET_ID,
    ATTENTION_DELIVERY_SET_ID,
    ATTENTION_ITEM_SET_ID,
    ATTENTION_PRESENTATION_SET_ID,
    CENTRAL_ATTENTION_REVISION,
)
from tools.graph.schemas.link_approval import (
    LINK_APPROVAL_INTENT_SET_ID,
    LINK_APPROVAL_RESULT_SET_ID,
)


logger = logging.getLogger(__name__)

PRIVATE_CENTRAL_SET_IDS = frozenset({
    APPROVAL_REQUEST_SET_ID,
    APPROVAL_RESOLUTION_SET_ID,
    ATTENTION_ITEM_SET_ID,
    ATTENTION_PRESENTATION_SET_ID,
    ATTENTION_DELIVERY_SET_ID,
    LINK_APPROVAL_INTENT_SET_ID,
    LINK_APPROVAL_RESULT_SET_ID,
})

_BROWSER_EVENT_SET_IDS = frozenset({
    ATTENTION_ITEM_SET_ID,
    ATTENTION_PRESENTATION_SET_ID,
})
_SSE_CLOSE = object()
_MAX_PENDING_CHANGES = 1024
_SUBSCRIBER_QUEUE_SIZE = 32
_HEARTBEAT_SECONDS = 15.0
_MAX_MUTATION_BODY_BYTES = 32 * 1024


def is_private_central_set_id(value: Any) -> bool:
    return isinstance(value, str) and value in PRIVATE_CENTRAL_SET_IDS


def _bounded_key(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("invalid Settings key")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("invalid Settings key") from exc
    if not 1 <= size <= 256 or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError("invalid Settings key")
    return value


@dataclass(frozen=True, slots=True)
class _ChangeEnvelope:
    set_id: str
    key: str
    operation: str


class PrivateAttentionHub:
    """One loop-owned bounded fanout with thread-safe synchronous ingress."""

    def __init__(
        self,
        *,
        item_resolver,
        max_pending: int = _MAX_PENDING_CHANGES,
        subscriber_queue_size: int = _SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        if not callable(item_resolver):
            raise ValueError("attention item resolver is required")
        if max_pending < 1 or subscriber_queue_size < 1:
            raise ValueError("attention hub bounds must be positive")
        self._item_resolver = item_resolver
        self._max_pending = max_pending
        self._subscriber_queue_size = subscriber_queue_size
        self._thread_lock = threading.Lock()
        self._pending: dict[tuple[str, str], _ChangeEnvelope] = {}
        self._gap_pending = False
        self._drain_scheduled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drain_task: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue] = set()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        with self._thread_lock:
            if self._loop is not None and self._loop is not loop:
                raise RuntimeError("attention hub already belongs to another loop")
            self._loop = loop

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        with self._thread_lock:
            owner = self._loop
            self._loop = None
            self._pending.clear()
            self._gap_pending = False
            self._drain_scheduled = False
        if owner is not None and owner is not loop:
            return
        task = self._drain_task
        self._drain_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for queue in tuple(self._subscribers):
            self._close_queue(queue)
        self._subscribers.clear()

    def emit_setting_change(
        self,
        *,
        operation: Any,
        snapshot: Any,
        org: Any,
    ) -> None:
        """Accept one trusted post-commit hint from any thread, best-effort."""
        try:
            if not isinstance(snapshot, Mapping):
                return
            set_id = snapshot.get("set_id")
            if not is_private_central_set_id(set_id):
                return
            if org is not None:
                return
            revision = snapshot.get("schema_revision")
            if (
                isinstance(revision, bool)
                or revision != CENTRAL_ATTENTION_REVISION
            ):
                return
            key = _bounded_key(snapshot.get("key"))
            if not isinstance(operation, str) or not operation:
                return
            envelope = _ChangeEnvelope(set_id=set_id, key=key, operation=operation)
        except Exception:
            return

        with self._thread_lock:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            if not self._gap_pending:
                change_key = (envelope.set_id, envelope.key)
                if change_key not in self._pending and len(self._pending) >= self._max_pending:
                    self._pending.clear()
                    self._gap_pending = True
                else:
                    self._pending[change_key] = envelope
            if self._drain_scheduled:
                return
            self._drain_scheduled = True
            try:
                loop.call_soon_threadsafe(self._begin_drain)
            except Exception:
                self._drain_scheduled = False
                self._pending.clear()
                self._gap_pending = False

    def emit_refresh(self, _attention_id: str | None = None) -> None:
        """Schedule one payload-free full refetch from any thread.

        Organization-owned Link result rows never enter browser frames.  Their
        reconciler uses this method only after exact personal/org correlation.
        Coalescing to the existing gap frame keeps the hint bounded and avoids
        disclosing the organization set address or result key.
        """
        with self._thread_lock:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            self._pending.clear()
            self._gap_pending = True
            if self._drain_scheduled:
                return
            self._drain_scheduled = True
            try:
                loop.call_soon_threadsafe(self._begin_drain)
            except Exception:
                self._drain_scheduled = False
                self._gap_pending = False

    def _begin_drain(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while True:
                with self._thread_lock:
                    gap = self._gap_pending
                    batch = tuple(self._pending.values())
                    self._gap_pending = False
                    self._pending.clear()
                    if not gap and not batch:
                        self._drain_scheduled = False
                        return
                if gap:
                    self._fanout("attention:refresh", {"attention_id": None})
                    continue
                for envelope in batch:
                    if envelope.set_id not in _BROWSER_EVENT_SET_IDS:
                        continue
                    await self._emit_browser_change(envelope)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("private attention hub drain failed")
            self._fanout("attention:refresh", {"attention_id": None})
        finally:
            with self._thread_lock:
                if self._loop is None:
                    self._pending.clear()
                    self._gap_pending = False
                    self._drain_scheduled = False
                elif self._pending or self._gap_pending:
                    asyncio.get_running_loop().call_soon(self._begin_drain)
                else:
                    self._drain_scheduled = False

    async def _emit_browser_change(self, envelope: _ChangeEnvelope) -> None:
        try:
            item = await asyncio.to_thread(self._item_resolver, envelope.key)
        except AttentionIndexError as exc:
            if exc.code == "invalid_request":
                return
            item = None
        except Exception:
            item = None
        if item is None:
            self._fanout("attention:refresh", {"attention_id": envelope.key})
            return
        if envelope.set_id == ATTENTION_PRESENTATION_SET_ID:
            self._fanout("attention:presentation", {"attention_id": envelope.key})
            return
        source_version = item.payload["source_version"]
        self._fanout("attention:changed", {
            "event_id": canonical_attention_event_id(envelope.key, source_version),
            "attention_id": envelope.key,
            "source_version": source_version,
        })

    def subscribe(self) -> asyncio.Queue:
        if self._loop is not asyncio.get_running_loop():
            raise RuntimeError("attention hub is not running on this loop")
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._subscriber_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if self._loop is not None and self._loop is not asyncio.get_running_loop():
            raise RuntimeError("attention hub unsubscribe must run on its owner loop")
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
    index: AttentionIndexService
    presentation: AttentionPresentationService
    approvals: ApprovalService
    hub: PrivateAttentionHub
    approval_http: ApprovalHttpBridge | None = None
    approval_reconciler: Any | None = None
    link_receipt_forwarder: Any | None = None
    operator_result_projectors: Mapping[str, Any] | None = None

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
                registry=ApprovalHttpRegistry(
                    approvals=self.approvals.registry,
                    attention=self.index.registry,
                ),
            )
        elif (
            self.approval_http.approvals is not self.approvals
            or self.approval_http.registry.approvals is not self.approvals.registry
            or self.approval_http.registry.attention is not self.index.registry
        ):
            raise ValueError("Central route runtime must share one exact composition")


def build_production_runtime() -> AttentionRouteRuntime:
    dashboard_approval_runtime = dashboard_access_central.build_approval_runtime()
    approval_registry = build_production_registry(runtimes={
        dashboard_access_central.KIND: dashboard_approval_runtime,
    })
    approval_waiters = ApprovalWaitHub()
    coordinator_holder: dict[str, Any] = {}

    def approval_after_commit(record_type: str, approval_id: str) -> None:
        approval_waiters.notify(record_type, approval_id)
        coordinator = coordinator_holder.get("coordinator")
        if coordinator is not None:
            coordinator.offer(approval_id)

    approvals = ApprovalService(
        registry=approval_registry,
        after_commit=approval_after_commit,
    )
    dashboard_attention_runtime = dashboard_access_central.build_attention_runtime(
        approvals,
    )
    runtimes = {
        (
            dashboard_access_central.KIND,
            dashboard_access_central.APPLICATION_SCOPE,
        ): dashboard_attention_runtime,
    }
    # The backup plugin's non-approval publication runtimes (auto-fnydv).
    # The registry rows are closed substrate code; the plugin supplies
    # only projection/evidence translation. Import failure degrades to
    # class_disabled for the backup classes, never a boot failure.
    try:
        from tools.dashboard.plugins.backup.attention import (
            publication_runtimes as backup_publication_runtimes,
        )
        runtimes.update(backup_publication_runtimes())
    except Exception:
        logging.getLogger(__name__).exception(
            "backup attention runtimes unavailable; backup classes stay "
            "disabled")
    attention_registry = build_production_attention_registry(
        approval_registry=approval_registry,
        runtimes=runtimes,
    )
    index = AttentionIndexService(registry=attention_registry)
    consumer = dashboard_access_central.DashboardAccessResultConsumer()
    producer = attention_registry.producer(
        dashboard_access_central.KIND,
        dashboard_access_central.APPLICATION_SCOPE,
    )
    coordinator = dashboard_access_central.DashboardAccessCoordinator(
        approvals=approvals,
        index=index,
        producer=producer,
        consumer=consumer,
    )
    coordinator_holder["coordinator"] = coordinator
    approval_http = ApprovalHttpBridge(
        approvals=approvals,
        registry=ApprovalHttpRegistry(
            approvals=approval_registry,
            attention=attention_registry,
            adapters={
                dashboard_access_central.KIND:
                    dashboard_access_central.build_http_adapter(
                        consumer,
                        reconcile=coordinator.reconcile_exact,
                    ),
            },
        ),
        wait_hub=approval_waiters,
    )
    return AttentionRouteRuntime(
        index=index,
        presentation=AttentionPresentationService(),
        approvals=approvals,
        hub=PrivateAttentionHub(item_resolver=index.get_query_item),
        approval_http=approval_http,
        approval_reconciler=coordinator,
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


def sync_registrations() -> int:
    return _runtime.index.sync_registrations()


def emit_setting_change(*, operation: str, snapshot: Mapping[str, Any], org: str | None) -> None:
    try:
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


def emit_personal_sync_change(*, addresses=(), gap: bool = False) -> None:
    """Accept one payload-free post-materialization hint from Fleet sync."""
    try:
        if _runtime.approval_reconciler is not None:
            _runtime.approval_reconciler.offer_synced(
                addresses=addresses,
                gap=gap,
            )
    except Exception:
        logger.warning("personal-sync approval hint failed", exc_info=True)
        try:
            if _runtime.approval_reconciler is not None:
                _runtime.approval_reconciler.offer_gap()
        except Exception:
            pass


def scrub_private_cached_events(bus: Any) -> int:
    discard = getattr(bus, "discard_cached", None)
    if not callable(discard):
        return 0

    def predicate(topic: str, data: Any, decoded_ok: bool) -> bool:
        if topic != "setting.changed":
            return False
        if not decoded_ok or not isinstance(data, Mapping):
            return True
        set_id = data.get("set_id")
        if not isinstance(set_id, str):
            return True
        return set_id in PRIVATE_CENTRAL_SET_IDS

    return int(discard(predicate))


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


def _presentation(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    source = {} if payload is None else payload
    return {
        "seen_at": source.get("seen_at"),
        "last_opened_at": source.get("last_opened_at"),
        "snoozed_until": source.get("snoozed_until"),
    }


def _safe_item(item: AttentionQueryItem) -> dict[str, Any]:
    payload = item.payload
    return {
        "attention_id": item.attention_id,
        "application": {
            "scope": payload["application_scope"],
            "label": item.application_label,
            "icon_ref": item.icon_ref,
        },
        "category": item.surface_category,
        "participant_role": payload["participant_role"],
        "attention_state": payload["attention_state"],
        "title": payload["safe_title"],
        "summary": payload.get("safe_summary"),
        "counterparty_ref": payload.get("counterparty_ref"),
        "occurred_at": payload["occurred_at"],
        "source_version": payload["source_version"],
        "presentation": _presentation(item.presentation),
        "open": {
            "mode": "registered_renderer",
            "renderer_id": item.review_renderer_id,
        },
    }


def _safe_resolution(record: Any) -> dict[str, Any] | None:
    if record is None:
        return None
    payload = record.payload
    return {"outcome": payload["outcome"], "resolved_at": payload["resolved_at"]}


def _serialize_query(result: AttentionQueryResult) -> dict[str, Any]:
    counts = result.counts
    return {
        "items": [_safe_item(item) for item in result.items],
        "counts": {
            "total_needs_attention": counts.total_needs_attention,
            "categories": dict(counts.categories),
            "states": dict(counts.states),
            "applications": {
                key: dict(value) for key, value in counts.applications.items()
            },
        },
        "next_cursor": result.next_cursor,
        "snapshot_version": result.snapshot_version,
    }


def _review_unavailable(item: AttentionQueryItem) -> JSONResponse:
    return _no_store(
        {"error": "review_unavailable", "item": _safe_item(item)},
        status_code=409,
    )


def _approval_context(
    item: AttentionQueryItem,
) -> tuple[Any, Any, ApprovalStatus] | None:
    payload = item.payload
    try:
        attention_registration = _runtime.index.registry.require_class(
            payload["application_scope"], payload["notification_class"],
        )
        approval_registration = _runtime.approvals.registry.require(
            attention_registration.kind, enabled=False,
        )
        if (
            attention_registration.runtime is None
            or approval_registration.runtime is None
            or approval_registration.renderer_id != item.review_renderer_id
            or approval_registration.notification_class != payload["notification_class"]
            or payload["application_scope"]
            not in approval_registration.application_scope_policy.applications
        ):
            return None
        status = _runtime.approvals.status(payload["object_ref"])
        request_payload = status.request.payload
        if (
            request_payload.get("source_version") != 1
            or request_payload.get("application_scope") != payload["application_scope"]
            or request_payload.get("kind") != attention_registration.kind
        ):
            return None
        role = payload["participant_role"]
        state = payload["attention_state"]
        version = payload["source_version"]
        resolution = status.resolution
        valid = (
            version == 1
            and ((role == "recipient" and state == "needs_attention")
                 or (role == "sender" and state == "waiting"))
        ) or (
            version == 2
            and state == "resolved"
            and role in {"recipient", "sender"}
            and resolution is not None
        )
        if not valid or (version == 2 and resolution is None):
            return None
        return attention_registration, approval_registration, status
    except ApprovalServiceError:
        raise
    except Exception as exc:
        raise AttentionIndexError("unavailable") from exc


async def api_attention_items(request: Request):
    denied = _operator_guard(request)
    if denied is not None:
        return denied
    allowed = {"application", "category", "state", "role", "limit", "cursor"}
    if any(key not in allowed for key in request.query_params):
        return _no_store({"error": "invalid_request"}, status_code=400)
    if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        return _no_store({"error": "invalid_request"}, status_code=400)
    limit_raw = request.query_params.get("limit")
    if limit_raw is None:
        limit = 50
    elif not limit_raw.isascii() or not limit_raw.isdecimal():
        return _no_store({"error": "invalid_request"}, status_code=400)
    else:
        try:
            limit = int(limit_raw)
        except Exception:
            return _no_store({"error": "invalid_request"}, status_code=400)
    try:
        result = await asyncio.to_thread(
            _runtime.index.query,
            application_scope=request.query_params.get("application"),
            surface_category=request.query_params.get("category"),
            attention_state=request.query_params.get("state"),
            participant_role=request.query_params.get("role"),
            limit=limit,
            cursor=request.query_params.get("cursor"),
        )
    except AttentionIndexError as exc:
        status = 409 if exc.code == "refresh_required" else (
            503 if exc.code == "unavailable" else 400
        )
        return _no_store({"error": exc.code}, status_code=status)
    return _no_store(_serialize_query(result))


async def _exact_item(attention_id: Any) -> AttentionQueryItem | None:
    return await asyncio.to_thread(_runtime.index.get_query_item, attention_id)


async def api_attention_item(request: Request):
    denied = _operator_guard(request)
    if denied is not None:
        return denied
    try:
        item = await _exact_item(request.path_params["attention_id"])
    except AttentionIndexError as exc:
        status = 503 if exc.code == "unavailable" else 400
        return _no_store({"error": exc.code}, status_code=status)
    if item is None:
        return _no_store({"error": "not_found"}, status_code=404)
    try:
        context = await asyncio.to_thread(_approval_context, item)
    except ApprovalServiceError as exc:
        if exc.code == "storage_unavailable":
            return _no_store({"error": "unavailable"}, status_code=503)
        return _review_unavailable(item)
    except AttentionIndexError:
        return _no_store({"error": "unavailable"}, status_code=503)
    if context is None:
        return _review_unavailable(item)
    _attention_registration, approval_registration, status = context
    request_payload = status.request.payload
    resolution = _safe_resolution(status.resolution)
    actions: list[str] = []
    if (
        item.payload["participant_role"] == "recipient"
        and item.payload["attention_state"] == "needs_attention"
        and item.payload["source_version"] == 1
        and status.resolution is None
    ):
        try:
            actor = resolve_human_approval_actor(request)
            expected = request_payload.get("decider")
            if (
                isinstance(expected, Mapping)
                and expected.get("kind") == "person"
                and expected.get("id") == actor.decider_ref
            ):
                actions = ["granted", "declined"]
        except ApprovalServiceError:
            pass
    requester = request_payload["requester_ref"]
    safe_requester = {"kind": requester["kind"]}
    if requester.get("label"):
        safe_requester["label"] = requester["label"]
    application_result = None
    has_application_result = False
    assert _runtime.operator_result_projectors is not None
    projector = _runtime.operator_result_projectors.get(approval_registration.kind)
    if projector is not None:
        has_application_result = True
        try:
            projected = await asyncio.to_thread(projector, status)
            if projected is not None:
                application_result = ApprovalHttpBridge._json_mapping(
                    projected,
                    code="unavailable",
                )
        except Exception:
            return _no_store({"error": "unavailable"}, status_code=503)
    review = {
        "type": "approval",
        "renderer_id": item.review_renderer_id,
        "kind": approval_registration.kind,
        "authority_requirement": approval_registration.authority_requirement.value,
        "safe_review": dict(request_payload["safe_review"]),
        "requester": safe_requester,
        "resolution": resolution,
        "actions": actions,
    }
    if has_application_result:
        review["application_result"] = application_result
    return _no_store({
        "item": _safe_item(item),
        "review": review,
    })


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
        item = await _exact_item(request.path_params["attention_id"])
    except AttentionIndexError as exc:
        status_code = 503 if exc.code == "unavailable" else 422
        return _no_store({"error": exc.code}, status_code=status_code)
    if item is None:
        return _no_store({"error": "not_found"}, status_code=404)
    try:
        actor = resolve_human_approval_actor(request)
    except ApprovalServiceError as exc:
        status_code = 503 if exc.code == "not_configured" else 401
        return _no_store({"error": exc.code}, status_code=status_code)
    try:
        context = await asyncio.to_thread(_approval_context, item)
    except ApprovalServiceError as exc:
        status_code = 503 if exc.code == "storage_unavailable" else 409
        return _no_store({"error": "review_unavailable"}, status_code=status_code)
    except AttentionIndexError:
        return _no_store({"error": "unavailable"}, status_code=503)
    if context is None:
        return _no_store({"error": "review_unavailable"}, status_code=409)
    _attention_registration, _approval_registration, status = context
    if item.payload["participant_role"] != "recipient":
        return _no_store({"error": "not_actionable"}, status_code=409)
    if not (
        item.payload["source_version"] == 1
        and item.payload["attention_state"] == "needs_attention"
    ) and not (
        item.payload["source_version"] == 2
        and item.payload["attention_state"] == "resolved"
        and status.resolution is not None
    ):
        return _no_store({"error": "review_unavailable"}, status_code=409)
    try:
        resolution = await asyncio.to_thread(
            _runtime.approvals.decide,
            item.payload["object_ref"],
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


async def api_attention_link_receipt(request: Request):
    """Forward one signed Link receipt request to its frozen registry.

    The handler accepts no destination, organization, path, approval kind, or
    store selector.  The inactive Link runtime supplies the item-bound
    forwarder in tests; production returns not-found until Link activation.
    """
    denied = operator_mutation_guard(request)
    if denied is not None:
        return denied
    try:
        body = await _strict_json_object(request)
    except ValueError:
        return _no_store({"error": "invalid_request"}, status_code=422)
    forwarder = _runtime.link_receipt_forwarder
    if forwarder is None:
        return _no_store({"error": "not_found"}, status_code=404)
    try:
        result = await asyncio.to_thread(
            forwarder.forward,
            request.path_params["attention_id"],
            body,
        )
    except Exception as exc:
        code = getattr(exc, "code", "unavailable")
        if code == "not_found":
            status = 404
        elif code in {"invalid_request", "invalid_decision", "receipt_invalid"}:
            status = 422
        elif code in {"not_actionable", "source_expired", "binding_drift"}:
            status = 409
        else:
            status = 503
            code = "unavailable"
        return _no_store({"error": code}, status_code=status)
    return _no_store(dict(result))


async def _presentation_mutation(request: Request, operation: str):
    denied = operator_mutation_guard(request)
    if denied is not None:
        return denied
    try:
        if operation == "snooze":
            body = await _strict_json_object(request)
            if set(body) != {"duration_seconds"}:
                raise ValueError
            call = _runtime.presentation.snooze
            args = (request.path_params["attention_id"], body["duration_seconds"])
        else:
            body = await _strict_json_object(request, allow_empty=True)
            if body:
                raise ValueError
            call = {
                "seen": _runtime.presentation.mark_seen,
                "opened": _runtime.presentation.mark_opened,
                "clear_snooze": _runtime.presentation.clear_snooze,
            }[operation]
            args = (request.path_params["attention_id"],)
        result = await asyncio.to_thread(call, *args)
    except ValueError:
        return _no_store({"error": "invalid_request"}, status_code=422)
    except AttentionPresentationError as exc:
        status = 404 if exc.code == "not_found" else (
            503 if exc.code == "unavailable" else 422
        )
        return _no_store({"error": exc.code}, status_code=status)
    return _no_store({"presentation": _presentation(result.payload)})


async def api_attention_seen(request: Request):
    return await _presentation_mutation(request, "seen")


async def api_attention_opened(request: Request):
    return await _presentation_mutation(request, "opened")


async def api_attention_snooze(request: Request):
    return await _presentation_mutation(request, "snooze")


async def api_attention_clear_snooze(request: Request):
    return await _presentation_mutation(request, "clear_snooze")


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
        "/api/attention/items/{attention_id:path}/link-operation-receipt",
        api_attention_link_receipt,
        methods=["POST"],
    ),
    Route(
        "/api/attention/items/{attention_id:path}/seen",
        api_attention_seen,
        methods=["POST"],
    ),
    Route(
        "/api/attention/items/{attention_id:path}/opened",
        api_attention_opened,
        methods=["POST"],
    ),
    Route(
        "/api/attention/items/{attention_id:path}/snooze",
        api_attention_snooze,
        methods=["POST"],
    ),
    Route(
        "/api/attention/items/{attention_id:path}/snooze",
        api_attention_clear_snooze,
        methods=["DELETE"],
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
