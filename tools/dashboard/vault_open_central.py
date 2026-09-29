"""Settings-native ``vault_open`` approval: release one secured Setting.

A session asks for one secured vault Setting (``graph vault read`` posts
``{"kind": "vault_open", "request": {set_id, key, ttl_seconds}}`` to
/api/approvals; the bridge routes the claimed kind here). The operator reviews
it in the Central inbox and grants it with the Setting's own factor ceremony.

The content key never rides Central. A Central resolution is a personal
Setting that replicates to every personal machine, so the decision is ``{}``
for grant and decline alike (auto-fkhq0.27, design checkpoint 2026-09-28).
The operator's browser runs the ceremony, holds the unwrapped content key
(CEK) in memory, records the Grant, and then posts the CEK to this Dashboard's
delivery endpoint (:class:`VaultOpenDelivery`). The endpoint opens the frozen
revision, writes the value only into the requesting session's private ramfs,
and records the value-free release lease as the receipt. Nothing here stores,
logs or echoes the CEK.

- Only the accepting machine delivers: the Dashboard the requesting session
  runs on (its frozen ``result_destination_id``). A Grant replicated to
  another machine delivers nothing there, and the review says which machine
  can deliver before the ceremony.
- A Grant is deliverable until the requesting session is no longer live or
  :data:`DELIVERY_WINDOW_SECONDS` after the Grant, whichever comes first.
  An interrupted delivery (Grant without receipt) is redelivered only by
  repeating the factor ceremony; no key material persists and nothing
  resumes on its own.
- The requester's result stays null until the receipt exists.

Mirrors mailbox_central.py (planner, attention projection, HTTP adapter,
coordinator).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable

from tools.dashboard import api_auth
from tools.dashboard import vault_open_approvals as vault
from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridgeError,
    ApprovalHttpKindAdapter,
    CanonicalLegacyDecision,
)
from tools.dashboard.approval_kind_registry import (
    ApprovalKindRuntime,
    ApprovalPlanningContext,
    ApprovalRequestPlan,
)
from tools.dashboard.approval_service import ApprovalService, ApprovalServiceError, ApprovalStatus
from tools.dashboard.attention_index_service import AttentionIndexError
from tools.dashboard.attention_registry import (
    AttentionProjectionPlan,
    AttentionPublicationRuntime,
    AttentionSourceEvidence,
)
from tools.dashboard.dashboard_access_central import (
    DashboardAccessCoordinator,
    _bounded_approval_id,
    _opaque_digest,
)
from tools.dashboard.mailbox_central import _requesting_session

logger = logging.getLogger(__name__)

KIND = "vault_open"
APPLICATION_SCOPE = "vault"
RENDERER_ID = "approval.vault_open.review"
CONSUMER_ID = "vault_open.local_delivery.v1"
#: How long after the Grant the operator may still post the content key.
DELIVERY_WINDOW_SECONDS = 1800
_DESTINATION_DOMAIN = b"dashboard.vault.open-delivery-destination.v1"
_ATTENTION_DOMAIN = "dashboard.attention.vault-open-recipient"
_REQUEST_FIELDS = {"set_id", "key", "ttl_seconds"}
_HEX = frozenset("0123456789abcdef")

#: Operator-facing delivery states (review.application_result.state).
PENDING = "pending"
AWAITING = "awaiting_delivery"
DELIVERED = "delivered"
FAILED = "delivery_failed"
EXPIRED = "expired_undelivered"
ELSEWHERE = "elsewhere"


class VaultOpenDeliveryError(RuntimeError):
    """A bounded delivery refusal. ``code`` is the whole public message."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def result_destination_id(secret: bytes | None = None) -> str:
    """This Dashboard's opaque identity as the machine that delivers."""
    if secret is None:
        from tools.dashboard import unlock_routes
        secret = unlock_routes._session_secret()
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Dashboard session secret is unavailable")
    digest = hmac.new(secret, _DESTINATION_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def this_machine_label() -> str:
    """The name the review shows for the machine that can deliver."""
    try:
        from tools.network import fleet_machine_profile, machine_boot
        machine = machine_boot.machine_id(org="machine")
        if machine:
            name = fleet_machine_profile.names().get(machine)
            if name:
                return str(name)[:80]
    except Exception:
        logger.debug("vault_open: machine name unavailable", exc_info=True)
    import socket
    return (socket.gethostname() or "this machine")[:80]


def vault_open_attention_id(approval_id: str) -> str:
    _bounded_approval_id(approval_id)
    return "attention-" + _opaque_digest([_ATTENTION_DOMAIN, 1, approval_id])


def _principal(context: ApprovalPlanningContext) -> api_auth.ApiPrincipal:
    session = _requesting_session(context)
    kind = api_auth.ApiPrincipalKind(context.requester_principal_kind)
    return api_auth.ApiPrincipal(
        kind, subject=session,
        org=context.requester_org if kind is api_auth.ApiPrincipalKind.ORG_SESSION else None,
    )


def build_request_planner(
    *,
    destination_resolver: Callable[[], str] = result_destination_id,
    machine_label: Callable[[], str] = this_machine_label,
    freeze: Callable[[api_auth.ApiPrincipal, dict], tuple[dict, dict]] = vault.freeze_request,
):
    def plan(context: ApprovalPlanningContext, body: Mapping[str, Any]) -> ApprovalRequestPlan:
        if not isinstance(body, Mapping) or not set(body) <= _REQUEST_FIELDS:
            raise ValueError("vault_open accepts only set_id, key and ttl_seconds")
        principal = _principal(context)
        request, staged = freeze(principal, dict(body))
        destination = destination_resolver()
        if not isinstance(destination, str) or len(destination) != 43:
            raise ValueError("this Dashboard cannot deliver a vault release right now")
        machine = machine_label()
        label = context.requester_ref.get("label") or principal.subject
        return ApprovalRequestPlan(
            subject_ref=f"vault-open:{context.approval_id}",
            safe_review={
                "title": "Release a vault item",
                "detail": f"{label} wants {request['target']}, delivered to it on {machine}.",
                "requester_label": label,
                "target": request["target"],
                "access": request["access"],
                "ttl_seconds": request["ttl_seconds"],
                "machine_label": machine,
            },
            request=request,
            staged={**staged, "result_destination_id": destination, "machine_label": machine},
        )

    return plan


def _validate_decision(_context, _request, decision, _is_grant) -> dict[str, Any]:
    # The CEK never rides a replicated Central resolution: it is posted to the
    # delivery endpoint after the Grant. Grant and decline carry nothing.
    if decision:
        raise ValueError("vault_open decisions carry no payload")
    return {}


def build_approval_runtime(**planner_options) -> ApprovalKindRuntime:
    return ApprovalKindRuntime(
        request_planner=build_request_planner(**planner_options),
        decision_validator=_validate_decision,
        resolution_consumer_id=CONSUMER_ID,
        result_ref_builder=lambda approval_id, _request, _decision: f"vault-open:{approval_id}",
    )


def build_attention_runtime(approvals: ApprovalService) -> AttentionPublicationRuntime:
    def plan(source: Any) -> AttentionProjectionPlan:
        if not isinstance(source, ApprovalStatus):
            raise ValueError("vault_open projection requires approval status")
        request = source.request.payload
        if request.get("kind") != KIND:
            raise ValueError("vault_open projection kind mismatch")
        resolution = source.resolution
        review = request.get("safe_review") or {}
        label = review.get("requester_label") or "A session"
        return AttentionProjectionPlan(
            attention_id=vault_open_attention_id(source.request.approval_id),
            object_ref=source.request.approval_id,
            participant_role="recipient",
            attention_state="resolved" if resolution is not None else "needs_attention",
            safe_title="Release a vault item",
            safe_summary=(f"{label} wants {review.get('target') or '?'} "
                          f"(deliverable only from {review.get('machine_label') or '?'})")[:240],
            counterparty_ref=None,
            occurred_at=(float(resolution.payload["resolved_at"]) if resolution is not None
                         else float(request["created_at"])),
            source_version=2 if resolution is not None else 1,
        )

    def evidence(object_ref: str, source_version: int) -> AttentionSourceEvidence:
        status = approvals.status(_bounded_approval_id(object_ref))
        actual = 2 if status.resolution is not None else 1
        if actual != source_version or status.request.payload.get("kind") != KIND:
            raise AttentionIndexError("stale_source")
        return AttentionSourceEvidence(
            source_guard={"kind": "approval", "ref": object_ref, "version": source_version},
            source_expires_at=status.request.payload.get("expires_at"),
        )

    return AttentionPublicationRuntime(projection_planner=plan, source_evidence_builder=evidence)


def _session_live(session: str | None) -> bool:
    if not isinstance(session, str) or not session:
        return False
    from tools.dashboard.dao import dashboard_db
    from tools.dashboard.session_lifecycle_worker import derive_lifecycle_state
    row = dashboard_db.get_session(session)
    return row is not None and derive_lifecycle_state(row) == "ACTIVE"


def _lease(approval_id: str) -> dict | None:
    from tools.dashboard.dao import vault_releases
    return vault_releases.get(approval_id)


def _notify(session: str | None, approval_id: str, *, status: str, summary: str, body: str) -> None:
    """Wake the requesting session (deduped by id). Never carries the value."""
    if not isinstance(session, str) or not session:
        return
    try:
        from tools.dashboard import session_notify
        session_notify.deliver_task_notification_sync(
            session, f"vault-open:{approval_id}", kind="vault-open", status=status,
            summary=summary, body=body,
        )
    except Exception:
        logger.warning("vault_open: requester wake failed for %s", approval_id)


class VaultOpenDelivery:
    """The accepting machine's delivery of a granted release, and its state."""

    _lock = threading.Lock()

    def __init__(
        self,
        *,
        approvals: ApprovalService,
        index: Any = None,
        destination_resolver: Callable[[], str] = result_destination_id,
        session_live: Callable[[str | None], bool] = _session_live,
        lease: Callable[[str], dict | None] = _lease,
        deliver: Callable[[str, dict, dict, bytearray], dict] = vault.open_and_deliver,
        ceremony: Callable[[dict, dict], dict] = vault.ceremony_for,
        notify: Callable[..., None] = _notify,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.approvals = approvals
        self.index = index
        self._destination_resolver = destination_resolver
        self._session_live = session_live
        self._lease = lease
        self._deliver = deliver
        self._ceremony = ceremony
        self._notify = notify
        self._clock = clock

    # ── state ───────────────────────────────────────────────────────────

    def _here(self, payload: Mapping[str, Any]) -> bool:
        staged = payload.get("staged")
        destination = staged.get("result_destination_id") if isinstance(staged, Mapping) else None
        try:
            ours = self._destination_resolver()
        except Exception:
            return False
        return isinstance(destination, str) and hmac.compare_digest(destination, ours)

    def _deliverable_until(self, status: ApprovalStatus) -> float | None:
        resolution = status.resolution
        if resolution is None or resolution.payload.get("outcome") != "granted":
            return None
        return float(resolution.payload["resolved_at"]) + DELIVERY_WINDOW_SECONDS

    def state(self, status: ApprovalStatus) -> str:
        payload = status.request.payload
        if payload.get("kind") != KIND:
            raise ValueError("not a vault_open approval")
        resolution = status.resolution
        if resolution is None:
            return PENDING if self._here(payload) else ELSEWHERE
        if resolution.payload.get("outcome") != "granted":
            return str(resolution.payload.get("outcome"))
        lease = self._lease(status.request.approval_id)
        if lease is not None:
            return FAILED if lease.get("shred_reason") == "delivery_failed" else DELIVERED
        if not self._here(payload):
            return ELSEWHERE
        requester = (payload.get("request") or {}).get("requester") or {}
        until = self._deliverable_until(status)
        if until is None or self._clock() >= until or not self._session_live(requester.get("session")):
            return EXPIRED
        return AWAITING

    @staticmethod
    def _receipt(lease: Mapping[str, Any], request: Mapping[str, Any]) -> dict:
        return {"release_id": lease["id"], "delivery": "session-ramfs",
                "path": lease["container_path"],
                "ttl_seconds": int(request.get("ttl_seconds") or 0)}

    def operator_result(self, status: ApprovalStatus) -> dict:
        """review.application_result for the Central renderer."""
        payload = status.request.payload
        state = self.state(status)
        result: dict[str, Any] = {
            "state": state,
            "machine_label": (payload.get("staged") or {}).get("machine_label") or "",
        }
        until = self._deliverable_until(status)
        if until is not None:
            result["deliverable_until"] = until
        if state == DELIVERED:
            lease = self._lease(status.request.approval_id) or {}
            result["path"] = lease.get("container_path", "")
        return result

    def requester_result(self, status: ApprovalStatus) -> dict | None:
        """The requester's envelope result: null until the receipt exists."""
        state = self.state(status)
        request = status.request.payload.get("request") or {}
        if state == DELIVERED:
            lease = self._lease(status.request.approval_id) or {}
            return {"approved": True, "execution": {"ok": True, "receipt": self._receipt(lease, request)}}
        if state == FAILED:
            return {"approved": True, "execution": {
                "ok": False, "error": "the release could not be written to the session"}}
        if state == EXPIRED:
            return {"approved": True, "execution": {
                "ok": False, "error": "the release was granted but not delivered in time; ask again"}}
        return None

    # ── operator actions ─────────────────────────────────────────────────

    def _approval_for_item(self, attention_id: Any) -> ApprovalStatus:
        if self.index is None:
            raise VaultOpenDeliveryError("not_found")
        try:
            item = self.index.get_query_item(attention_id)
        except ValueError:
            raise VaultOpenDeliveryError("not_found") from None
        except Exception:
            raise VaultOpenDeliveryError("unavailable") from None
        if item is None:
            raise VaultOpenDeliveryError("not_found")
        approval_id = item.payload.get("object_ref")
        try:
            status = self.approvals.status(_bounded_approval_id(approval_id))
        except (ApprovalServiceError, ValueError):
            raise VaultOpenDeliveryError("not_found") from None
        if status.request.payload.get("kind") != KIND or \
                item.attention_id != vault_open_attention_id(status.request.approval_id):
            raise VaultOpenDeliveryError("not_found")
        return status

    def bootstrap(self, attention_id: Any) -> dict:
        """The factor ceremony and open bundle, for the accepting machine's
        operator, while the request is pending or granted and undelivered."""
        status = self._approval_for_item(attention_id)
        state = self.state(status)
        if state == ELSEWHERE:
            raise VaultOpenDeliveryError("elsewhere")
        if state not in (PENDING, AWAITING):
            raise VaultOpenDeliveryError("not_actionable")
        payload = status.request.payload
        try:
            return self._ceremony(payload["request"], payload["staged"])
        except Exception:
            raise VaultOpenDeliveryError("binding_drift") from None

    def deliver(self, attention_id: Any, body: Any) -> dict:
        """Open the granted revision with the operator's CEK and deliver it.

        Every refusal is a fixed code. The body is never interpolated into an
        error or a log line, and the CEK is zeroed before this returns.
        """
        if not isinstance(body, Mapping) or set(body) != {"content_key"}:
            raise VaultOpenDeliveryError("invalid_request")
        raw = body.get("content_key")
        if not isinstance(raw, str) or len(raw) != 64 or not set(raw) <= _HEX:
            raise VaultOpenDeliveryError("invalid_request")
        content_key = bytearray.fromhex(raw)
        try:
            status = self._approval_for_item(attention_id)
            approval_id = status.request.approval_id
            payload = status.request.payload
            with self._lock:
                state = self.state(status)
                if state == DELIVERED:
                    lease = self._lease(approval_id) or {}
                    return {"receipt": self._receipt(lease, payload.get("request") or {})}
                if state == ELSEWHERE:
                    raise VaultOpenDeliveryError("elsewhere")
                if state == EXPIRED:
                    raise VaultOpenDeliveryError("window_closed")
                if state == FAILED:
                    raise VaultOpenDeliveryError("delivery_failed")
                if state != AWAITING:
                    raise VaultOpenDeliveryError("not_actionable")
                request = payload["request"]
                try:
                    vault.assert_frozen(request, payload["staged"])
                except Exception:
                    raise VaultOpenDeliveryError("binding_drift") from None
                try:
                    receipt = self._deliver(approval_id, request, payload["staged"], content_key)
                except VaultOpenDeliveryError:
                    raise
                except Exception as exc:
                    # Never the exception's text: it may quote the request body.
                    if self._lease(approval_id) is not None:
                        logger.warning("vault_open: delivery into the session failed for %s (%s)",
                                       approval_id, type(exc).__name__)
                        raise VaultOpenDeliveryError("delivery_failed") from None
                    logger.info("vault_open: the content key did not open %s (%s)",
                                approval_id, type(exc).__name__)
                    raise VaultOpenDeliveryError("open_failed") from None
            name = (request.get("setting") or {}).get("key") or "secret"
            self._notify(
                (request.get("requester") or {}).get("session"), approval_id,
                status="released", summary=f"Vault secret released: {name}",
                body=(f"The approved secret is at {receipt.get('path')}. It was written when "
                      "the operator approved it, so its TTL clock has started."),
            )
            return {"receipt": dict(receipt)}
        finally:
            content_key[:] = b"\x00" * len(content_key)


def build_http_adapter(
    delivery: VaultOpenDelivery,
    *,
    reconcile: Callable[[str], ApprovalStatus | None] | None = None,
) -> ApprovalHttpKindAdapter:
    def project_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = payload.get("request")
        if not isinstance(request, Mapping):
            raise RuntimeError("vault_open request is unavailable")
        return {"target": request.get("target"), "ttl_seconds": request.get("ttl_seconds")}

    def map_decision(body: Mapping[str, Any]) -> CanonicalLegacyDecision:
        # The legacy decision route carried the CEK; it is not accepted here.
        if body == {"approved": False}:
            return CanonicalLegacyDecision("declined", {})
        raise ApprovalHttpBridgeError("invalid_decision")

    def project_result(status: ApprovalStatus) -> Mapping[str, Any] | None:
        if reconcile is not None:
            refreshed = reconcile(status.request.approval_id)
            if refreshed is not None:
                status = refreshed
        return delivery.requester_result(status)

    return ApprovalHttpKindAdapter(kind=KIND, request_projector=project_request,
                                   result_projector=project_result,
                                   legacy_decision_mapper=map_decision)


class VaultOpenCoordinator(DashboardAccessCoordinator):
    """The dashboard-access wake coordinator, reconciling ``vault_open``.

    It publishes the attention item and wakes the requester on a decline or
    expiry. It never delivers: delivery needs the operator's CEK."""

    def __init__(self, *, delivery: VaultOpenDelivery, **kwargs) -> None:
        super().__init__(consumer=delivery, **kwargs)
        self.delivery = delivery

    def reconcile_exact(self, approval_id: str) -> ApprovalStatus | None:
        try:
            status = self.approvals.status(_bounded_approval_id(approval_id))
        except ApprovalServiceError as exc:
            if exc.code == "not_found":
                return None
            raise
        payload = status.request.payload
        if payload.get("kind") != KIND:
            return None
        self.index.publish(self.producer, status)
        resolution = status.resolution
        if resolution is not None and resolution.payload.get("outcome") in ("declined", "expired") \
                and self.delivery._here(payload):
            request = payload.get("request") or {}
            name = (request.get("setting") or {}).get("key") or "secret"
            outcome = resolution.payload["outcome"]
            self.delivery._notify(
                (request.get("requester") or {}).get("session"), status.request.approval_id,
                status="declined",
                summary=f"Vault secret release {outcome}: {name}",
                body="The release was not approved. Re-run the read to ask again.",
            )
        return status


__all__ = [
    "APPLICATION_SCOPE", "CONSUMER_ID", "DELIVERY_WINDOW_SECONDS", "KIND", "RENDERER_ID",
    "VaultOpenCoordinator", "VaultOpenDelivery", "VaultOpenDeliveryError",
    "build_approval_runtime", "build_attention_runtime", "build_http_adapter",
    "result_destination_id", "this_machine_label", "vault_open_attention_id",
]
