"""Settings-native ``link_publish`` / ``link_revoke`` approvals (auto-fkhq0.10a).

An agent or the CLI asks to publish or revoke a share link
(``graph link publish``, ``graph follow publish``, ``graph link revoke``: POST
/api/approvals ``{kind, request}``; the bridge routes the claimed kinds here).
The operator reviews it in the Central inbox. A request the operator makes in
their own browser does not come here; it has no approval to make
(link_operation_routes.py).

Authority and transport are today's (link_operations.py): the operator's
browser signs the frozen registry request with the persona-certified org
session key, the envelope is verified locally, and the tunnel carries it out.
The envelope is an authorization and Central resolutions replicate to every
personal machine, so grant and decline carry ``{}``. After the Grant the
browser signs and posts the envelope to this Dashboard's operation endpoint
(:class:`LinkApprovalDesk`), which runs it once and records the result in the
machine-homed link-operation journal.

- Only the accepting machine operates: the Dashboard that received the
  request (its frozen ``result_destination_id``).
- The envelope must be signed no earlier than the Grant.
- The requester's result stays null until the operation is recorded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from typing import Any, Callable

from tools.dashboard import link_operations as ops
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
from tools.dashboard.dashboard_access_central import (
    _bounded_approval_id,
)
from tools.dashboard.vault_open_central import this_machine_label

PUBLISH_KIND = "link_publish"
REVOKE_KIND = "link_revoke"
KINDS = (PUBLISH_KIND, REVOKE_KIND)
APPLICATION_SCOPE = "links"
CONSUMER_ID = "link.tunnel_operation.v1"
_DESTINATION_DOMAIN = b"dashboard.links.operation-destination.v1"

PENDING = "pending"
AWAITING = "awaiting_operation"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
ELSEWHERE = "elsewhere"


def result_destination_id(secret: bytes | None = None) -> str:
    """This Dashboard's opaque identity as the machine that operates."""
    if secret is None:
        from tools.dashboard import unlock_routes
        secret = unlock_routes._session_secret()
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Dashboard session secret is unavailable")
    digest = hmac.new(secret, _DESTINATION_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_request_planner(
    kind: str,
    *,
    destination_resolver: Callable[[], str] = result_destination_id,
    machine_label: Callable[[], str] = this_machine_label,
    planner: Callable[[str, Any], dict] = ops.plan,
):
    op = ops.op_for_kind(kind)

    def plan(context: ApprovalPlanningContext, body: Mapping[str, Any]) -> ApprovalRequestPlan:
        # The organization whose link this is travels as ``org_slug``: Central
        # reserves ``org`` for routing selectors it derives itself. Here it is
        # application data the operator reviews and the persona signature
        # binds (verified against that org's binding).
        # An org-bound caller may name only its own org (review of .10a): the
        # same rule api_auth applies to every org-bound request. A local
        # session (a host terminal, the operator's own) names any org this
        # node holds. The target is resolved only in the org settled here.
        body = dict(body)
        named = body.pop("org_slug", None)
        if context.requester_principal_kind == "org_session":
            if named not in (None, context.requester_org):
                raise ValueError("an organization session publishes only in its own organization")
            org = context.requester_org
        else:
            org = named
        if not isinstance(org, str) or not org:
            raise ValueError("a link request names its organization as org_slug")
        request = {"org": org, **body}
        try:
            planned = planner(op, request)
        except ops.LinkOperationError as exc:
            raise ValueError(exc.detail or exc.code) from None
        destination = destination_resolver()
        if not isinstance(destination, str) or len(destination) != 43:
            raise ValueError("this Dashboard cannot operate links right now")
        machine = machine_label()
        label = context.requester_ref.get("label") or "A session"
        review = planned["review"]
        verb = "publish" if op == ops.PUBLISH else "revoke"
        noun = review.get("type_label") or "share link"
        request = planned["request"]
        expires_ms = request.get("expires_at") if request.get("target_type") == "org:join" else None
        return ApprovalRequestPlan(
            subject_ref=f"link-{verb}:{context.approval_id}",
            safe_review={
                **review,
                "title": f"{verb.capitalize()} a share link",
                "detail": f"{label} wants to {verb} a link to {review.get('target_title') or noun}.",
                "requester_label": label,
                "machine_label": machine,
            },
            request=request,
            staged={**planned["staged"], "result_destination_id": destination,
                    "machine_label": machine},
            trusted_source_expires_at=(float(expires_ms) / 1000.0
                                       if isinstance(expires_ms, (int, float)) else None),
        )

    return plan


def _validate_decision(_context, _request, decision, _is_grant) -> dict[str, Any]:
    # The signed envelope never rides a replicated Central resolution: it is
    # posted to the operation endpoint after the Grant.
    if decision:
        raise ValueError("link decisions carry no payload")
    return {}


def build_approval_runtime(kind: str, **planner_options) -> ApprovalKindRuntime:
    return ApprovalKindRuntime(
        request_planner=build_request_planner(kind, **planner_options),
        decision_validator=_validate_decision,
        resolution_consumer_id=CONSUMER_ID,
        result_ref_builder=lambda approval_id, _request, _decision: f"{kind}:{approval_id}",
    )


def inbox_text(status: ApprovalStatus) -> tuple[str, str | None]:
    """The inbox's title and summary for one approval of this kind."""
    review = status.request.payload.get("safe_review") or {}
    return review.get("title") or "Share link", review.get("detail") or None


class LinkApprovalDesk:
    """The accepting machine's operation for a granted link approval."""

    def __init__(
        self,
        *,
        approvals: ApprovalService,
        destination_resolver: Callable[[], str] = result_destination_id,
        journal: type = ops.Journal,
        verify: Callable[..., tuple[dict, str]] = ops.verify,
        execute: Callable[..., Any] = ops.execute,
        signing_view: Callable[[dict, dict], dict] = ops.signing_view,
    ) -> None:
        self.approvals = approvals
        self._destination_resolver = destination_resolver
        self._journal = journal
        self._verify = verify
        self._execute = execute
        self._signing_view = signing_view

    def _here(self, payload: Mapping[str, Any]) -> bool:
        staged = payload.get("staged")
        destination = staged.get("result_destination_id") if isinstance(staged, Mapping) else None
        try:
            ours = self._destination_resolver()
        except Exception:
            return False
        return isinstance(destination, str) and hmac.compare_digest(destination, ours)

    def _entry(self, status: ApprovalStatus) -> dict | None:
        return ops.read(status.request.approval_id, journal=self._journal)

    def state(self, status: ApprovalStatus) -> str:
        payload = status.request.payload
        if payload.get("kind") not in KINDS:
            raise ValueError("not a link approval")
        resolution = status.resolution
        if resolution is None:
            return PENDING if self._here(payload) else ELSEWHERE
        if resolution.payload.get("outcome") != "granted":
            return str(resolution.payload.get("outcome"))
        entry = self._entry(status)
        if entry is not None and entry.get("state") in (DONE, FAILED):
            return entry["state"]
        if entry is not None and entry.get("state") == "claimed":
            return RUNNING
        if not self._here(payload):
            return ELSEWHERE
        return AWAITING

    def operator_result(self, status: ApprovalStatus) -> dict:
        payload = status.request.payload
        state = self.state(status)
        result: dict[str, Any] = {
            "state": state,
            "machine_label": (payload.get("staged") or {}).get("machine_label") or "",
        }
        if state in (DONE, FAILED):
            result["execution"] = (self._entry(status) or {}).get("execution")
        return result

    def requester_result(self, status: ApprovalStatus) -> dict | None:
        state = self.state(status)
        if state in (DONE, FAILED):
            return {"approved": True, "execution": (self._entry(status) or {}).get("execution")}
        return None

    def _approval(self, approval_id: Any) -> ApprovalStatus:
        try:
            status = self.approvals.status(_bounded_approval_id(approval_id))
        except (ApprovalServiceError, ValueError):
            raise ops.LinkOperationError("not_found") from None
        if status.request.payload.get("kind") not in KINDS:
            raise ops.LinkOperationError("not_found")
        return status

    def bootstrap(self, approval_id: Any) -> dict:
        status = self._approval(approval_id)
        state = self.state(status)
        if state == ELSEWHERE:
            raise ops.LinkOperationError("elsewhere")
        if state not in (PENDING, AWAITING):
            raise ops.LinkOperationError("not_actionable")
        payload = status.request.payload
        return self._signing_view(payload["request"], payload["staged"])

    async def operate(self, approval_id: Any, body: Any) -> dict:
        status = self._approval(approval_id)
        payload = status.request.payload
        approval_id = status.request.approval_id
        state = self.state(status)
        if state in (DONE, FAILED):
            return {"execution": (self._entry(status) or {}).get("execution")}
        if state == RUNNING:
            raise ops.LinkOperationError("running")
        if state == ELSEWHERE:
            raise ops.LinkOperationError("elsewhere")
        if state != AWAITING:
            raise ops.LinkOperationError("not_actionable")
        op = ops.op_for_kind(payload["kind"])
        granted_at = float(status.resolution.payload["resolved_at"])
        decision, persona = self._verify(op, payload["request"], payload["staged"], body,
                                         not_before=granted_at)
        entry = self._journal.get(approval_id) or {
            "state": "prepared", "op": op, "initiator": f"approval:{approval_id}",
            "prepared_at": granted_at, "request": payload["request"],
            "staged": {k: payload["staged"][k]
                       for k in ("method", "path", "registry_url", "payload", "binding")},
        }
        execution = await self._execute(approval_id, entry, decision, persona,
                                        journal=self._journal)
        return {"execution": execution}


def build_http_adapter(kind: str, desk: LinkApprovalDesk) -> ApprovalHttpKindAdapter:
    def project_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = payload.get("request")
        if not isinstance(request, Mapping):
            raise RuntimeError("link request is unavailable")
        return {k: request.get(k) for k in ("org", "target_type", "target_uuid") if k in request} \
            or {"org": request.get("org")}

    def map_decision(body: Mapping[str, Any]) -> CanonicalLegacyDecision:
        # The legacy decision carried the signed envelope; it is not accepted here.
        if body == {"approved": False}:
            return CanonicalLegacyDecision("declined", {})
        raise ApprovalHttpBridgeError("invalid_decision")

    def project_result(status: ApprovalStatus) -> Mapping[str, Any] | None:
        return desk.requester_result(status)

    return ApprovalHttpKindAdapter(kind=kind, request_projector=project_request,
                                   result_projector=project_result,
                                   legacy_decision_mapper=map_decision)


__all__ = [
    "APPLICATION_SCOPE", "KINDS", "LinkApprovalDesk",
    "PUBLISH_KIND", "REVOKE_KIND", "build_approval_runtime",
    "inbox_text", "build_http_adapter", "result_destination_id",
]
