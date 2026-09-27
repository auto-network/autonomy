"""Settings-native ``email_send`` approval: one email, reviewed, sent once.

A session asks to send one plain-text email from its organization's mailbox
capability (``mail-send`` posts ``{"kind": "email_send", "request": {to, cc,
subject, body}}`` to /api/approvals; the bridge routes the claimed kind here).
The operator reviews it in the shared approval dialog (central-attention.js):
From (resolved host-side from the org install, never from the request), To,
Cc, Subject and the exact body. Operator-session authority: no signing.

Exactly once: the Central request and resolution are personal Settings and
replicate to every personal machine. Only the machine that accepted the
request sends (its frozen ``result_destination_id``), and the machine-homed
``autonomy.machine.mailbox-send`` row is claimed before the SMTP call, so a
replay or a restart never sends twice; a claim without an outcome becomes
``unknown`` and is not retried.

Mirrors dashboard_access_central.py (planner, attention projection, HTTP
adapter, coordinator); the differences are the operator-session decision
(``{}`` for grant and decline) and the send journal.
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

from agents.capabilities.mailbox.backend import api as mail
from tools.dashboard import api_auth
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
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    ApprovalStatus,
    canonical_session_requester_id,
)
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
from tools.graph import settings_ops
from tools.graph.schemas.central_attention import APPROVAL_REQUEST_SET_ID, ApprovalRequestV1
from tools.graph.schemas.mailbox_send import MAILBOX_SEND_REVISION, MAILBOX_SEND_SET_ID

logger = logging.getLogger(__name__)

KIND = "email_send"
APPLICATION_SCOPE = "mailbox"
RENDERER_ID = "approval.email_send.review"
CONSUMER_ID = "email_send.local_send.v1"
_DESTINATION_DOMAIN = b"dashboard.mailbox.email-send-destination.v1"
_ATTENTION_DOMAIN = "dashboard.attention.email-send-recipient"
_REQUEST_FIELDS = {"to", "cc", "subject", "body"}


def result_destination_id(secret: bytes | None = None) -> str:
    """This Dashboard's opaque identity as the machine that will send."""
    if secret is None:
        from tools.dashboard import unlock_routes
        secret = unlock_routes._session_secret()
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Dashboard session secret is unavailable")
    digest = hmac.new(secret, _DESTINATION_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def email_send_attention_id(approval_id: str) -> str:
    _bounded_approval_id(approval_id)
    return "attention-" + _opaque_digest([_ATTENTION_DOMAIN, 1, approval_id])


def _requesting_session(context: ApprovalPlanningContext) -> str:
    """The requesting session's name, proven from the frozen requester identity.

    The label starts with the session handle (attention_routes.
    session_requester_label); it is only a hint until the scope-bound identity
    recomputed from it equals the requester id the service derived from the
    bearer."""
    requester = context.requester_ref or {}
    label = requester.get("label")
    if not isinstance(label, str) or not label:
        raise ValueError("the requesting session is unknown")
    subject = label.split(" · ", 1)[0]
    kind = context.requester_principal_kind
    try:
        principal_kind = api_auth.ApiPrincipalKind(kind)
    except ValueError as exc:
        raise ValueError("email_send is requested by a session") from exc
    principal = api_auth.ApiPrincipal(
        principal_kind, subject=subject,
        org=context.requester_org if principal_kind is api_auth.ApiPrincipalKind.ORG_SESSION else None,
    )
    try:
        proven = canonical_session_requester_id(principal) == requester.get("id")
    except ApprovalServiceError:
        proven = False
    if not proven:
        raise ValueError("the requesting session could not be proven")
    return subject


def _workspace_enabled(workspace: str, org: str | None) -> bool:
    from agents.workspace_settings import WORKSPACE_CAPABILITY_ENABLE_SET_ID
    from tools.graph import ops as graph_ops
    rows = graph_ops.read_set(WORKSPACE_CAPABILITY_ENABLE_SET_ID, org=org, peers=[])
    for m in rows.members:
        if m.key == f"{workspace}:{mail.CONTRACT}":
            payload = m.payload if isinstance(m.payload, dict) else {}
            return payload.get("enabled", True) is not False
    return False


def build_request_planner(
    *,
    destination_resolver: Callable[[], str] = result_destination_id,
    workspace_resolver: Callable[[str], tuple[str, str | None]] | None = None,
    sender_resolver: Callable[[str | None], str] = mail.sender_address,
):
    def resolve_workspace(session: str) -> tuple[str, str | None]:
        if workspace_resolver is not None:
            return workspace_resolver(session)
        from tools.dashboard import mailbox_routes
        workspace = mailbox_routes._require_enabled(session)
        from agents.workspace_settings import get_workspace
        return workspace, get_workspace(workspace).graph_project

    def plan(context: ApprovalPlanningContext, body: Mapping[str, Any]) -> ApprovalRequestPlan:
        if not isinstance(body, Mapping) or not set(body) <= _REQUEST_FIELDS:
            raise ValueError("email_send accepts only to, cc, subject and body")
        fields = mail.validate_send(str(body.get("to") or ""), str(body.get("subject") or ""),
                                    str(body.get("body") or ""), str(body.get("cc") or ""))
        lines = mail.body_lines(fields["body"])
        session = _requesting_session(context)
        workspace, workspace_org = resolve_workspace(session)
        org = context.requester_org
        sender = sender_resolver(org)
        destination = destination_resolver()
        if not isinstance(destination, str) or len(destination) != 43:
            raise ValueError("this Dashboard cannot send email right now")
        label = context.requester_ref.get("label") or session
        message = {"to": fields["to"], "cc": fields["cc"], "subject": fields["subject"],
                   "body_lines": lines}
        return ApprovalRequestPlan(
            subject_ref=f"email-send:{context.approval_id}",
            safe_review={
                "title": "Send email",
                "detail": f"{label} wants to send an email from {sender}.",
                "requester_label": label,
                "from_addr": sender,
                **message,
            },
            request=message,
            staged={
                "from_addr": sender,
                "org": org,
                "workspace": workspace,
                "workspace_org": workspace_org,
                "result_destination_id": destination,
            },
        )

    return plan


def _validate_decision(_context, _request, decision, _is_grant) -> dict[str, Any]:
    # Operator-session authority: the decision itself carries nothing.
    if decision:
        raise ValueError("email_send decisions carry no payload")
    return {}


def build_approval_runtime(**planner_options) -> ApprovalKindRuntime:
    return ApprovalKindRuntime(
        request_planner=build_request_planner(**planner_options),
        decision_validator=_validate_decision,
        resolution_consumer_id=CONSUMER_ID,
        result_ref_builder=lambda approval_id, _request, _decision: f"email-send:{approval_id}",
    )


def build_attention_runtime(approvals: ApprovalService) -> AttentionPublicationRuntime:
    def plan(source: Any) -> AttentionProjectionPlan:
        if not isinstance(source, ApprovalStatus):
            raise ValueError("email_send projection requires approval status")
        request = source.request.payload
        if request.get("kind") != KIND:
            raise ValueError("email_send projection kind mismatch")
        resolution = source.resolution
        review = request.get("safe_review") or {}
        label = review.get("requester_label") or "A session"
        subject = review.get("subject") or "(no subject)"
        return AttentionProjectionPlan(
            attention_id=email_send_attention_id(source.request.approval_id),
            object_ref=source.request.approval_id,
            participant_role="recipient",
            attention_state="resolved" if resolution is not None else "needs_attention",
            safe_title="Send email",
            safe_summary=f"{label} wants to email {review.get('to') or '?'}: {subject}"[:240],
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


class EmailSendConsumer:
    """Sends an approved email once, on the machine that accepted the request."""

    _lock = threading.Lock()
    # Sends running in this process. A "claimed" row that is not in flight
    # here was left by a stopped process: that is the only "unknown" case.
    _inflight: set = set()

    def __init__(
        self,
        *,
        destination_resolver: Callable[[], str] = result_destination_id,
        sender: Callable[..., dict] | None = None,
        enabled: Callable[[str, str | None], bool] = _workspace_enabled,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._destination_resolver = destination_resolver
        self._sender = sender or self._send
        self._enabled = enabled
        self._clock = clock

    @staticmethod
    def _send(org: str | None, *, to: str, cc: str, subject: str, body: str) -> dict:
        cfg = mail.MailboxConfig.resolve(org)
        return mail.send_message(cfg, to=to, cc=cc, subject=subject, body=body)

    @staticmethod
    def journal(approval_id: str) -> dict | None:
        row = settings_ops.read_set_key(MAILBOX_SEND_SET_ID, approval_id, org="machine", peers=[])
        payload = (row or {}).get("payload")
        return dict(payload) if isinstance(payload, dict) else None

    @staticmethod
    def _record(approval_id: str, payload: dict) -> None:
        settings_ops.write_by_key(MAILBOX_SEND_SET_ID, MAILBOX_SEND_REVISION, approval_id,
                                  {k: v for k, v in payload.items() if v is not None},
                                  org="machine")

    def _ours(self, status: ApprovalStatus) -> Mapping[str, Any] | None:
        request = status.request.payload
        resolution = status.resolution
        if request.get("kind") != KIND or resolution is None:
            return None
        if resolution.payload.get("outcome") != "granted":
            return None
        staged = request.get("staged")
        if not isinstance(staged, Mapping):
            raise RuntimeError("email_send staging is unavailable")
        destination = staged.get("result_destination_id")
        if not isinstance(destination, str) or not hmac.compare_digest(
                destination, self._destination_resolver()):
            return None
        return staged

    def materialize(self, status: ApprovalStatus) -> bool:
        staged = self._ours(status)
        if staged is None:
            return False
        approval_id = status.request.approval_id
        with self._lock:
            if approval_id in self._inflight:
                return True
            existing = self.journal(approval_id)
            if existing is not None:
                if existing.get("state") == "claimed":
                    # The process stopped between the claim and the outcome:
                    # the email may or may not have left. Never resend.
                    self._record(approval_id, {**existing, "state": "unknown",
                                               "finished_at": self._clock(),
                                               "error": "the send was interrupted; it was not retried"})
                return True
            self._record(approval_id, {"state": "claimed", "claimed_at": self._clock()})
            self._inflight.add(approval_id)
        message = status.request.payload.get("request") or {}
        outcome: dict[str, Any]
        try:
            if not self._enabled(str(staged.get("workspace") or ""), staged.get("workspace_org")):
                raise mail.MailboxError("the mailbox capability is no longer enabled for "
                                        f"workspace {staged.get('workspace')!r}")
            sent = self._sender(staged.get("org"), to=message.get("to", ""),
                                cc=message.get("cc", ""), subject=message.get("subject", ""),
                                body="\n".join(message.get("body_lines") or []) + "\n")
            outcome = {"state": "sent", "message_id": str(sent.get("message_id") or "")}
        except mail.MailboxError as e:
            outcome = {"state": "failed", "error": str(e)[:1000]}
        except Exception as e:  # never leave a claim without an outcome
            logger.exception("email_send failed")
            outcome = {"state": "failed", "error": f"{type(e).__name__}"}
        with self._lock:
            try:
                claim = self.journal(approval_id) or {}
                self._record(approval_id, {**claim, **outcome, "finished_at": self._clock()})
            finally:
                self._inflight.discard(approval_id)
        return True

    def project(self, status: ApprovalStatus) -> Mapping[str, Any] | None:
        if self._ours(status) is None:
            return None
        self.materialize(status)
        row = self.journal(status.request.approval_id) or {}
        state = row.get("state")
        if state == "sent":
            staged = status.request.payload.get("staged") or {}
            message = status.request.payload.get("request") or {}
            return {"approved": True, "execution": {
                "ok": True, "message_id": row.get("message_id", ""),
                "from": staged.get("from_addr", ""), "to": message.get("to", ""),
                "subject": message.get("subject", "")}}
        if state in ("failed", "unknown"):
            return {"approved": True, "execution": {"ok": False, "error": row.get("error", state)}}
        return None


def build_http_adapter(
    consumer: EmailSendConsumer,
    *,
    reconcile: Callable[[str], ApprovalStatus | None] | None = None,
) -> ApprovalHttpKindAdapter:
    def project_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = payload.get("request")
        if not isinstance(request, Mapping):
            raise RuntimeError("email_send request is unavailable")
        return {k: request.get(k) for k in ("to", "cc", "subject")}

    def map_decision(body: Mapping[str, Any]) -> CanonicalLegacyDecision:
        if body == {"approved": False}:
            return CanonicalLegacyDecision("declined", {})
        if body == {"approved": True}:
            return CanonicalLegacyDecision("granted", {})
        raise ApprovalHttpBridgeError("invalid_decision")

    def project_result(status: ApprovalStatus) -> Mapping[str, Any] | None:
        if reconcile is not None:
            refreshed = reconcile(status.request.approval_id)
            if refreshed is not None:
                status = refreshed
        return consumer.project(status)

    return ApprovalHttpKindAdapter(kind=KIND, request_projector=project_request,
                                   result_projector=project_result,
                                   legacy_decision_mapper=map_decision)


class EmailSendCoordinator(DashboardAccessCoordinator):
    """The dashboard-access wake coordinator, reconciling ``email_send``."""

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
            self.consumer.materialize(status)
        return status

    def _scan_ids(self) -> tuple[str, ...]:
        rows = settings_ops.read_set(APPROVAL_REQUEST_SET_ID, org=None, peers=[])
        if any(rows.dropped.values()):
            raise RuntimeError("partial Central approval request read")
        selected = []
        for row in rows:
            if not isinstance(row.payload, dict):
                raise RuntimeError("invalid Central approval request row")
            ApprovalRequestV1.validate(row.payload)
            if row.payload.get("kind") == KIND:
                selected.append(_bounded_approval_id(row.key))
        return tuple(sorted(set(selected)))


class ReconcilerGroup:
    """Several kind coordinators behind the runtime's one reconciler slot."""

    def __init__(self, *members: Any) -> None:
        self.members = members

    async def start(self) -> None:
        for m in self.members:
            await m.start()

    async def stop(self) -> None:
        for m in reversed(self.members):
            await m.stop()

    def offer(self, approval_id: Any) -> None:
        for m in self.members:
            m.offer(approval_id)

    def offer_gap(self) -> None:
        for m in self.members:
            m.offer_gap()

    def offer_local_setting(self, **kwargs) -> None:
        for m in self.members:
            m.offer_local_setting(**kwargs)

    def offer_synced(self, **kwargs) -> None:
        addresses = tuple(kwargs.pop("addresses", ()) or ())
        for m in self.members:
            m.offer_synced(addresses=addresses, **kwargs)

    def reconcile_exact(self, approval_id: str) -> ApprovalStatus | None:
        for m in self.members:
            status = m.reconcile_exact(approval_id)
            if status is not None:
                return status
        return None


__all__ = [
    "APPLICATION_SCOPE", "CONSUMER_ID", "EmailSendConsumer", "EmailSendCoordinator",
    "KIND", "RENDERER_ID", "ReconcilerGroup", "build_approval_runtime",
    "build_attention_runtime", "build_http_adapter", "email_send_attention_id",
    "result_destination_id",
]
