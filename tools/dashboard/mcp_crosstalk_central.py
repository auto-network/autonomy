"""Settings-native ``mcp_crosstalk``: a ChatGPT chat asks to message a session.

The MCP relay (authenticated by its service secret, mcp_relay_routes) opens a
Central approval carrying the exact message the chat wants to send; the
operator reads it in the Central inbox and grants a per-(chat, target) grant
for a lifetime, or declines (auto-fkhq0.14, design checkpoint 2026-09-28).

- The chat's raw ``openai/session`` is bearer-equivalent and never rides
  Central: the request carries the chat's handle and the target; the local
  grant row (mcp_relay_db) is found by its approval id.
- What is delivered is exactly what was reviewed: the message is frozen in
  ``safe_review.message_lines`` at creation and delivered from there. A
  message the review cannot hold whole is refused at creation, never
  truncated.
- Applied once, on the accepting machine only, within
  :data:`APPLY_WINDOW_SECONDS` of the resolution: the guarded grant
  transition and the outcome row commit together
  (``mcp_relay_db.settle_crosstalk``), then the message is delivered at most
  once. A delivery interrupted by a crash ends ``delivery_failed``
  (interrupted), never redelivered.
- Message content replicates with the Central request to every machine
  that syncs the operator's Central approvals (and so is visible to every
  approver of the organization).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any, Callable

from tools.dashboard import api_auth
from tools.dashboard.approval_kind_registry import (
    ApprovalKindRuntime,
    ApprovalPlanningContext,
    ApprovalRequestPlan,
)
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    ApprovalStatus,
    _ApprovalLocks,
)
from tools.dashboard.attention_index_service import AttentionIndexError
from tools.dashboard.attention_registry import (
    AttentionProjectionPlan,
    AttentionPublicationRuntime,
    AttentionSourceEvidence,
)
from tools.dashboard.dao import mcp_relay_db as db
from tools.dashboard.dashboard_access_central import (
    DashboardAccessCoordinator,
    _bounded_approval_id,
    _opaque_digest,
)
from tools.dashboard.vault_open_central import this_machine_label

logger = logging.getLogger(__name__)

KIND = "mcp_crosstalk"
APPLICATION_SCOPE = "relay"
RENDERER_ID = "approval.mcp_crosstalk.review"
CONSUMER_ID = "mcp_crosstalk.local_delivery.v1"
#: The relay's identity as a Central requester (after _relay_auth).
RELAY_SUBJECT = "mcp-relay"
RELAY_LABEL = "ChatGPT relay"
#: A Grant is applied only this long after the resolution.
APPLY_WINDOW_SECONDS = 1800
MAX_MESSAGE_BYTES = 6000
MAX_MESSAGE_LINES = 120
#: ApprovalRequestV1's own bound on safe_review, measured the same way.
_SAFE_REVIEW_MAX_BYTES = 8192
_DESTINATION_DOMAIN = b"dashboard.relay.crosstalk-delivery-destination.v1"
_ATTENTION_DOMAIN = "dashboard.attention.mcp-crosstalk-recipient"
_REQUEST_FIELDS = {"handle", "target_session", "target_org", "intent", "message"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_TEXT_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: Operator-facing states (review.application_result.state).
PENDING = "pending"
AWAITING = "awaiting_delivery"
DELIVERING = "delivering"
DELIVERED = "delivered"
FAILED = "delivery_failed"
EXPIRED = "expired_undelivered"
SUPERSEDED = "superseded"
ELSEWHERE = "elsewhere"


class CrosstalkRefused(ValueError):
    """A request the operator could not review whole; the reason is public."""


def relay_principal() -> api_auth.ApiPrincipal:
    return api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.MCP_SERVICE, subject=RELAY_SUBJECT)


def service_label(subject: str) -> str | None:
    return RELAY_LABEL if subject == RELAY_SUBJECT else None


def result_destination_id(secret: bytes | None = None) -> str:
    """This Dashboard's opaque identity as the machine that delivers."""
    if secret is None:
        from tools.dashboard import unlock_routes
        secret = unlock_routes._session_secret()
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Dashboard session secret is unavailable")
    digest = hmac.new(secret, _DESTINATION_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def crosstalk_attention_id(approval_id: str) -> str:
    _bounded_approval_id(approval_id)
    return "attention-" + _opaque_digest([_ATTENTION_DOMAIN, 1, approval_id])


def message_lines(message: str) -> list[str]:
    """The message as the operator will read it and the target will get it:
    tabs become four spaces, line endings are normalized, other control
    characters are refused."""
    text = (message or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    if not text.strip():
        raise CrosstalkRefused("the message is empty")
    if _CONTROL_RE.search(text):
        raise CrosstalkRefused("the message contains control characters")
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise CrosstalkRefused(
            f"the message is over {MAX_MESSAGE_BYTES} bytes; send a shorter message")
    lines = text.rstrip("\n").split("\n")
    if len(lines) > MAX_MESSAGE_LINES:
        raise CrosstalkRefused(
            f"the message is over {MAX_MESSAGE_LINES} lines; send a shorter message")
    return lines


def _text(value: Any, name: str, maximum: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if len(text) > maximum or _TEXT_CONTROL_RE.search(text):
        raise CrosstalkRefused(f"{name} is invalid")
    return text


def _target_label(target: str) -> str:
    try:
        from tools.dashboard.dao import dashboard_db
        session = dashboard_db.get_session(target)
    except Exception:
        return ""
    return str((session or {}).get("label") or "")[:200]


def build_request_planner(
    *,
    destination_resolver: Callable[[], str] = result_destination_id,
    machine_label: Callable[[], str] = this_machine_label,
    target_label: Callable[[str], str] = _target_label,
):
    def plan(context: ApprovalPlanningContext, body: Mapping[str, Any]) -> ApprovalRequestPlan:
        if context.requester_principal_kind != api_auth.ApiPrincipalKind.MCP_SERVICE.value:
            raise ValueError("mcp_crosstalk is requested by the MCP relay")
        if not isinstance(body, Mapping) or set(body) != _REQUEST_FIELDS:
            raise ValueError("mcp_crosstalk carries handle, target_session, target_org, "
                             "intent and message")
        review = crosstalk_review(body, machine=machine_label(), label=target_label(
            _text(body.get("target_session"), "target_session", 200)))
        destination = destination_resolver()
        if not isinstance(destination, str) or len(destination) != 43:
            raise ValueError("this Dashboard cannot deliver right now")
        return ApprovalRequestPlan(
            subject_ref=f"mcp-crosstalk:{context.approval_id}",
            safe_review=review,
            request={"handle": review["handle"], "target_session": review["target_session"],
                     "target_org": review["target_org"]},
            staged={"result_destination_id": destination,
                    "machine_label": review["machine_label"]},
        )

    return plan


def crosstalk_review(body: Mapping[str, Any], *, machine: str, label: str) -> dict:
    """The exact safe_review for this request, refused with a public reason
    when the operator could not review it whole."""
    handle = _text(body.get("handle"), "handle", 200)
    target = _text(body.get("target_session"), "target_session", 200)
    if not handle or not target:
        raise CrosstalkRefused("handle and target_session are required")
    review = {
        "title": "Message a session",
        "detail": f"{handle} wants to send a message to {label or target}.",
        "requester_label": RELAY_LABEL,
        "handle": handle,
        "target_session": target,
        "target_label": label,
        "target_org": _text(body.get("target_org"), "target_org", 200),
        "intent": _text(body.get("intent"), "intent", 1000),
        "message_lines": message_lines(body.get("message")),
        "machine_label": machine,
    }
    encoded = json.dumps(review, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _SAFE_REVIEW_MAX_BYTES:
        raise CrosstalkRefused("the message is too long to review whole; send a shorter message")
    if any(len(line) > 4096 for line in review["message_lines"]):
        raise CrosstalkRefused("a line of the message is too long; break it up")
    return review


def _validate_decision(_context, _request, decision, is_grant) -> dict[str, Any]:
    if not is_grant:
        if decision:
            raise ValueError("a decline carries no payload")
        return {}
    if set(decision) != {"ttl_seconds"}:
        raise ValueError("a grant carries exactly ttl_seconds")
    ttl = decision["ttl_seconds"]
    if ttl is not None and (type(ttl) is not int or ttl <= 0 or ttl > 10 * 365 * 86400):
        raise ValueError("ttl_seconds is a positive integer or null")
    return {"ttl_seconds": ttl}


def build_approval_runtime(**planner_options) -> ApprovalKindRuntime:
    return ApprovalKindRuntime(
        request_planner=build_request_planner(**planner_options),
        decision_validator=_validate_decision,
        resolution_consumer_id=CONSUMER_ID,
        result_ref_builder=lambda approval_id, _request, _decision: f"mcp-crosstalk:{approval_id}",
    )


def build_attention_runtime(approvals: ApprovalService) -> AttentionPublicationRuntime:
    def plan(source: Any) -> AttentionProjectionPlan:
        if not isinstance(source, ApprovalStatus):
            raise ValueError("mcp_crosstalk projection requires approval status")
        request = source.request.payload
        if request.get("kind") != KIND:
            raise ValueError("mcp_crosstalk projection kind mismatch")
        resolution = source.resolution
        review = request.get("safe_review") or {}
        return AttentionProjectionPlan(
            attention_id=crosstalk_attention_id(source.request.approval_id),
            object_ref=source.request.approval_id,
            participant_role="recipient",
            attention_state="resolved" if resolution is not None else "needs_attention",
            safe_title="Message a session",
            safe_summary=(f"{review.get('handle') or 'A chat'} → "
                          f"{review.get('target_label') or review.get('target_session') or '?'}")[:240],
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


def _owner() -> tuple[int, str | None]:
    import os
    from tools.dashboard.connector_key_resolution import process_start
    pid = os.getpid()
    return pid, process_start(pid)


def _owner_alive(row: Mapping[str, Any]) -> bool:
    from tools.dashboard.connector_key_resolution import process_start
    pid, start = row.get("owner_pid"), row.get("owner_start")
    if not isinstance(pid, int) or not start:
        return False
    return process_start(pid) == start


async def _deliver(handle: str, target: str, message: str) -> dict:
    from tools.dashboard import crosstalk_delivery
    return await crosstalk_delivery.deliver_from_chat(handle, target, message)


class CrosstalkDesk:
    """The accepting machine's once-only application of a crosstalk decision."""

    def __init__(
        self,
        *,
        approvals: ApprovalService,
        destination_resolver: Callable[[], str] = result_destination_id,
        deliver: Callable[[str, str, str], Any] = _deliver,
        owner: Callable[[], tuple[int, str | None]] = _owner,
        owner_alive: Callable[[Mapping[str, Any]], bool] = _owner_alive,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.approvals = approvals
        self._destination_resolver = destination_resolver
        self._deliver = deliver
        self._owner = owner
        self._owner_alive = owner_alive
        self._clock = clock

    def open(self, body: Mapping[str, Any]) -> str:
        """Open the Central approval for one held message (relay-authenticated)."""
        return self.approvals.create_from_principal(KIND, relay_principal(), dict(body)).approval_id

    def is_open(self, approval_id: str | None) -> bool:
        if not approval_id:
            return False
        try:
            return self.approvals.status(approval_id).resolution is None
        except (ApprovalServiceError, ValueError):
            return False

    def _here(self, payload: Mapping[str, Any]) -> bool:
        staged = payload.get("staged")
        destination = staged.get("result_destination_id") if isinstance(staged, Mapping) else None
        try:
            ours = self._destination_resolver()
        except Exception:
            return False
        return isinstance(destination, str) and hmac.compare_digest(destination, ours)

    def state(self, status: ApprovalStatus) -> tuple[str, str]:
        payload = status.request.payload
        if payload.get("kind") != KIND:
            raise ValueError("not an mcp_crosstalk approval")
        here = self._here(payload)
        resolution = status.resolution
        if resolution is None:
            return (PENDING if here else ELSEWHERE), ""
        if not here:
            return ELSEWHERE, ""
        outcome = db.get_outcome(status.request.approval_id)
        if outcome is not None:
            state = outcome["state"]
            return (AWAITING if state == DELIVERING else state), outcome.get("detail") or ""
        if resolution.payload.get("outcome") != "granted":
            return str(resolution.payload.get("outcome")), ""
        if self._clock() >= float(resolution.payload["resolved_at"]) + APPLY_WINDOW_SECONDS:
            return EXPIRED, ""
        return AWAITING, ""

    def operator_result(self, status: ApprovalStatus) -> dict:
        state, reason = self.state(status)
        result: dict[str, Any] = {
            "state": state,
            "machine_label": (status.request.payload.get("staged") or {}).get("machine_label") or "",
        }
        if reason:
            result["reason"] = reason
        return result

    def apply(self, approval_id: str) -> None:
        """Apply the resolution to the grant waiting on it, once. Sync: the
        coordinator and the relay routes run it in a worker thread."""
        with _ApprovalLocks.for_id(_bounded_approval_id(approval_id)):
            try:
                status = self.approvals.status(approval_id)
            except ApprovalServiceError:
                return
            payload = status.request.payload
            resolution = status.resolution
            if payload.get("kind") != KIND or resolution is None or not self._here(payload):
                return
            outcome = resolution.payload.get("outcome")
            if outcome != "granted":
                db.settle_crosstalk(
                    approval_id, status=db.DENIED if outcome == "declined" else "expired",
                    outcome=str(outcome))
                return
            now = self._clock()
            if now >= float(resolution.payload["resolved_at"]) + APPLY_WINDOW_SECONDS:
                db.settle_crosstalk(approval_id, status="expired", outcome=EXPIRED)
                return
            ttl = resolution.payload["decision"].get("ttl_seconds")
            pid, start = self._owner()
            grant = db.settle_crosstalk(
                approval_id, status=db.APPROVED, outcome=DELIVERING,
                expires_at=now + ttl if ttl is not None else None,
                owner_pid=pid, owner_start=start)
            if grant is None:
                # Applied already (the outcome exists and is kept), or the grant
                # was re-pointed at a newer approval: this one can never apply.
                db.record_outcome(approval_id, SUPERSEDED)
                return
            review = payload["safe_review"]
            if grant["target_session"] != review["target_session"]:
                db.finish_outcome(approval_id, FAILED, "target changed", expect=DELIVERING)
                return
            # The chat's handle is written once (mcp_relay_db.ensure_handle) and
            # never changes, so the stamped source is the reviewed one.
            session = db.get_session(grant["openai_session"]) or {}
            handle = session.get("handle") or payload["request"]["handle"]
            text = "\n".join(review["message_lines"])
            try:
                asyncio.run(self._deliver(handle, grant["target_session"], text))
            except Exception as exc:
                logger.warning("mcp_crosstalk: delivery failed for %s (%s)",
                               approval_id, type(exc).__name__)
                db.finish_outcome(approval_id, FAILED, "the target could not be reached",
                                  expect=DELIVERING)
                return
            db.finish_outcome(approval_id, DELIVERED, expect=DELIVERING)

    def recover_interrupted(self) -> None:
        """A delivery whose process is gone ended without an outcome: record it
        failed (interrupted). It is never redelivered."""
        for row in db.outcomes_in_state(DELIVERING):
            if not self._owner_alive(row):
                db.finish_outcome(row["approval_id"], FAILED, "interrupted", expect=DELIVERING)


class CrosstalkCoordinator(DashboardAccessCoordinator):
    """The dashboard-access wake coordinator for ``mcp_crosstalk``: publishes
    the attention item and applies a resolution on the accepting machine."""

    def __init__(self, *, desk: CrosstalkDesk, **kwargs) -> None:
        super().__init__(consumer=None, **kwargs)
        self.desk = desk

    def reconcile_exact(self, approval_id: str) -> ApprovalStatus | None:
        try:
            status = self.approvals.status(_bounded_approval_id(approval_id))
        except ApprovalServiceError as exc:
            if exc.code == "not_found":
                return None
            raise
        if status.request.payload.get("kind") != KIND:
            return None
        self.index.publish(self.producer, status)
        if status.resolution is not None:
            self.desk.apply(approval_id)
        return status


__all__ = [
    "APPLICATION_SCOPE", "APPLY_WINDOW_SECONDS", "CONSUMER_ID", "KIND", "RENDERER_ID",
    "CrosstalkCoordinator", "CrosstalkDesk", "CrosstalkRefused", "build_approval_runtime",
    "build_attention_runtime", "crosstalk_attention_id", "crosstalk_review", "message_lines",
    "relay_principal", "result_destination_id", "service_label",
]
