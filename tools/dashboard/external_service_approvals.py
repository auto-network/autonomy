"""Settings-native ``external_service_access``: a narrowly scoped device bearer.

A registered producer (the dropbox enrollment route) opens a Central approval
for an external device; the operator reviews the application, its exact API
capabilities and the requested lifetime in the Central inbox, and grants it
with a chosen ``{ttl_seconds}`` (auto-fkhq0.26, design checkpoint 2026-09-28).
A public client never chooses its audience, methods or paths: they are the
registration's (:data:`APPLICATIONS`).

The bearer never rides Central. A Central request and resolution are personal
Settings that replicate to every personal machine, so the bearer is minted
lazily, on the accepting machine, when the DEVICE polls with its poll secret:

- The poll secret is the id the device got back from enrollment. It never
  leaves this machine: only its sha256, bound to the public Central approval
  id, is kept in auth.db (``service_enrollments``). The Central approval id is
  the public correlation id and mints nothing.
- Each poll after the Grant, while the granted lifetime lasts, rotates: the previous bearer of the exact name
  ``<prefix>:<approval_id>`` is revoked and a new one inserted in one
  transaction, and the raw bearer is returned in that response only. The
  device is its only holder, so a rotation loses nothing and a stolen earlier
  response stops working.
- Once the granted lifetime is over a poll answers ``expired`` and mints
  nothing; an enrollment nothing can be collected from any more is forgotten
  the next time a device enrolls.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Mapping
from typing import Any, Callable

from tools.dashboard.approval_kind_registry import (
    ApprovalKindRuntime,
    ApprovalPlanningContext,
    ApprovalRequestPlan,
    RegisteredApprovalProducer,
)
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    ApprovalStatus,
    _ApprovalLocks,
)
from tools.dashboard.dao import auth_db
from tools.dashboard.dashboard_access_central import (
    _bounded_approval_id,
)
from tools.dashboard.vault_open_central import this_machine_label

logger = logging.getLogger(__name__)

KIND = "external_service_access"
RENDERER_ID = "approval.external_service_access.review"
CONSUMER_ID = "external_service_access.device_collect.v1"
MAX_TTL_SECONDS = 10 * 365 * 24 * 60 * 60
#: How long after the Grant the device may collect (and rotate) its bearer.
_DESTINATION_DOMAIN = b"dashboard.external-service.collect-destination.v1"

DROPBOX_PRODUCER = RegisteredApprovalProducer(
    "external_service.dropbox_enrollment", "Autonomy Capture", frozenset({KIND}),
)

# Registry-owned application identity, audience and exact API capabilities,
# keyed by the producer's application scope. The device supplies only a label
# and a requested lifetime.
APPLICATIONS = {
    "dropbox": {
        "producer_id": DROPBOX_PRODUCER.producer_id,
        "application": "Autonomy Capture",
        "summary": "Add screenshots to the global operator dropbox",
        "bearer_name_prefix": "dropbox-upload",
        "resource_audience": "global_operator_dropbox",
        "capabilities": [{"method": "POST", "path": "/api/dropbox"}],
    },
}

#: Operator-facing states (review.application_result.state).
PENDING = "pending"
AWAITING = "awaiting_collection"
DELIVERED = "delivered"
ELSEWHERE = "elsewhere"


def _valid_ttl(value: object, *, allow_none: bool = True) -> int | None:
    if value is None and allow_none:
        return None
    if type(value) is not int or value <= 0 or value > MAX_TTL_SECONDS:
        raise ValueError(
            "ttl_seconds must be a positive integer no greater than 10 years, "
            "or null for no expiry"
        )
    return value


def result_destination_id(secret: bytes | None = None) -> str:
    """This Dashboard's opaque identity as the machine the device polls."""
    if secret is None:
        from tools.dashboard import unlock_routes
        secret = unlock_routes._session_secret()
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Dashboard session secret is unavailable")
    digest = hmac.new(secret, _DESTINATION_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def poll_hash(poll_secret: str) -> str:
    return hashlib.sha256(poll_secret.encode("utf-8")).hexdigest()


def bearer_name(application_scope: str, approval_id: str) -> str:
    return f"{APPLICATIONS[application_scope]['bearer_name_prefix']}:{approval_id}"


def build_request_planner(
    *,
    destination_resolver: Callable[[], str] = result_destination_id,
    machine_label: Callable[[], str] = this_machine_label,
):
    def plan(context: ApprovalPlanningContext, body: Mapping[str, Any]) -> ApprovalRequestPlan:
        scope = context.application_scope
        spec = APPLICATIONS.get(scope)
        if spec is None or context.producer_id != spec["producer_id"]:
            raise ValueError("unknown external service application scope")
        if not isinstance(body, Mapping) or set(body) != {"label", "requested_ttl_seconds"}:
            raise ValueError("an enrollment carries only a label and a requested lifetime")
        raw_label = body.get("label")
        label = raw_label.strip() if isinstance(raw_label, str) else ""
        if not label or len(label) > 120:
            raise ValueError("requester label must be between 1 and 120 characters")
        requested_ttl = _valid_ttl(body.get("requested_ttl_seconds"))
        destination = destination_resolver()
        if not isinstance(destination, str) or len(destination) != 43:
            raise ValueError("this Dashboard cannot enroll a device right now")
        machine = machine_label()
        capabilities = auth_db.normalize_api_capabilities(spec["capabilities"])
        access = {
            "application": spec["application"],
            "summary": spec["summary"],
            "resource_audience": spec["resource_audience"],
            "capabilities": capabilities,
            "requested_ttl_seconds": requested_ttl,
        }
        return ApprovalRequestPlan(
            subject_ref=f"external-service:{context.approval_id}",
            safe_review={
                "title": "Allow service access",
                "detail": f"{label} asks to {spec['summary'].lower()}.",
                "requester_label": label,
                "machine_label": machine,
                **access,
            },
            request={"label": label, **access},
            staged={"application_scope": scope, "result_destination_id": destination,
                    "machine_label": machine},
        )

    return plan


def _validate_decision(_context, _request, decision, is_grant) -> dict[str, Any]:
    # The chosen lifetime is not secret; the bearer is minted at the device's
    # poll, never carried here.
    if not is_grant:
        if decision:
            raise ValueError("a decline carries no payload")
        return {}
    if set(decision) != {"ttl_seconds"}:
        raise ValueError("a grant carries exactly ttl_seconds")
    return {"ttl_seconds": _valid_ttl(decision["ttl_seconds"])}


def build_approval_runtime(**planner_options) -> ApprovalKindRuntime:
    return ApprovalKindRuntime(
        request_planner=build_request_planner(**planner_options),
        decision_validator=_validate_decision,
        resolution_consumer_id=CONSUMER_ID,
        result_ref_builder=lambda approval_id, _request, _decision: f"external-service:{approval_id}",
    )


def inbox_text(status: ApprovalStatus) -> tuple[str, str | None]:
    """The inbox's title and summary for one approval of this kind."""
    review = status.request.payload.get("safe_review") or {}
    return "Allow service access", f"{review.get('requester_label') or 'A device'} · {review.get('application') or 'External service'}"


class EnrollmentDesk:
    """The accepting machine's enrollments: open, collect, state, prune."""

    def __init__(
        self,
        *,
        approvals: ApprovalService,
        destination_resolver: Callable[[], str] = result_destination_id,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.approvals = approvals
        self._destination_resolver = destination_resolver
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

    @staticmethod
    def _lifetime_ends(status: ApprovalStatus) -> float | None:
        """When the granted access ends; None for a lifetime without end."""
        ttl = (status.resolution.payload.get("decision") or {}).get("ttl_seconds")
        if not isinstance(ttl, int):
            return None
        return float(status.resolution.payload["resolved_at"]) + ttl

    def _minted(self, status: ApprovalStatus) -> bool:
        scope = (status.request.payload.get("staged") or {}).get("application_scope")
        return scope in APPLICATIONS and auth_db.scoped_service_token_minted(
            bearer_name(scope, status.request.approval_id))

    def state(self, status: ApprovalStatus) -> str:
        payload = status.request.payload
        if payload.get("kind") != KIND:
            raise ValueError("not an external_service_access approval")
        here = self._here(payload)
        resolution = status.resolution
        if resolution is None:
            return PENDING if here else ELSEWHERE
        if resolution.payload.get("outcome") != "granted":
            return str(resolution.payload.get("outcome"))
        if not here:
            return ELSEWHERE
        return DELIVERED if self._minted(status) else AWAITING

    def operator_result(self, status: ApprovalStatus) -> dict:
        """review.application_result for the Central renderer. Never a bearer."""
        return {
            "state": self.state(status),
            "machine_label": (status.request.payload.get("staged") or {}).get("machine_label") or "",
        }

    # ── the device ──────────────────────────────────────────────────────

    def open(self, application_scope: str, poll_secret: str, body: Mapping[str, Any]) -> str:
        """Open a Central approval for a device; bind its poll secret's hash."""
        spec = APPLICATIONS.get(application_scope)
        if spec is None or spec["producer_id"] != DROPBOX_PRODUCER.producer_id:
            raise ValueError("unknown external service application scope")
        record = self.approvals.create_from_producer(KIND, DROPBOX_PRODUCER, dict(body))
        auth_db.insert_service_enrollment(poll_hash(poll_secret), record.approval_id)
        return record.approval_id

    def approval_for(self, poll_secret: Any) -> str | None:
        if not isinstance(poll_secret, str) or not 24 <= len(poll_secret) <= 128:
            return None
        return auth_db.service_enrollment_for(poll_hash(poll_secret))

    def pending_count(self) -> int:
        count = 0
        for approval_id in auth_db.service_enrollment_approvals():
            try:
                if self.approvals.status(approval_id).resolution is None:
                    count += 1
            except ApprovalServiceError:
                continue
        return count

    def prune(self) -> None:
        """Forget enrollments nothing can be collected from any more: declined,
        canceled or expired, or granted with the granted lifetime over. Run
        when a device enrolls, never on a timer."""
        now = self._clock()
        for approval_id in auth_db.service_enrollment_approvals():
            try:
                status = self.approvals.status(approval_id)
            except ApprovalServiceError as exc:
                if exc.code == "not_found":
                    auth_db.delete_service_enrollment(approval_id)
                continue
            resolution = status.resolution
            if resolution is None:
                continue
            if resolution.payload.get("outcome") == "granted":
                ends = self._lifetime_ends(status)
                if ends is None or now < ends:
                    continue
            auth_db.delete_service_enrollment(approval_id)

    def collect(self, approval_id: str) -> dict:
        """What the device's poll answers. Mints (rotating) only on a Grant,
        on this machine, while the granted lifetime lasts; the bearer is in this
        return only."""
        with _ApprovalLocks.for_id(_bounded_approval_id(approval_id)):
            status = self.approvals.status(approval_id)
            state = self.state(status)
            if state in (PENDING, ELSEWHERE):
                return {"status": "pending"}
            if state in ("declined", "canceled"):
                return {"status": "declined"}
            ends = self._lifetime_ends(status) if state in (AWAITING, DELIVERED) else None
            if state == "expired" or (ends is not None and self._clock() >= ends):
                return {"status": "expired"}
            payload = status.request.payload
            scope = payload["staged"]["application_scope"]
            spec = APPLICATIONS[scope]
            ttl = status.resolution.payload["decision"].get("ttl_seconds")
            raw = secrets.token_urlsafe(32)
            expires_at = self._clock() + ttl if ttl is not None else None
            auth_db.rotate_scoped_service_token(
                hashlib.sha256(raw.encode()).hexdigest(),
                bearer_name(scope, approval_id),
                capabilities=spec["capabilities"],
                application_scope=scope,
                resource_audience=spec["resource_audience"],
                source_approval_id=approval_id,
                expires_at=expires_at,
            )
            logger.info("external_service_access: bearer issued for %s", approval_id)
            return {
                "status": "approved",
                "token": raw,
                "expires_at": expires_at,
                "sourceApprovalId": approval_id,
                "application_scope": scope,
                "resource_audience": spec["resource_audience"],
                "capabilities": auth_db.normalize_api_capabilities(spec["capabilities"]),
            }


__all__ = [
    "APPLICATIONS", "CONSUMER_ID", "DROPBOX_PRODUCER", "KIND",
    "RENDERER_ID", "EnrollmentDesk", "bearer_name",
    "build_approval_runtime", "inbox_text", "poll_hash", "result_destination_id",
]
