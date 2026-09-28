"""Settings-native ``visitor_token`` approval: let one person into the missions.

A session asks for a guest link (``{"kind": "visitor_token", "request":
{display_name, avatar?, reason?}}`` to /api/approvals; the bridge routes the
claimed kind here). The operator sees who is being let in -- the face, the
name, why -- in the Central inbox and decides with operator-session authority.

The token is a bearer credential, so it never rides Central. A Central
request and resolution are personal Settings that replicate to every
personal machine, so the decision is ``{}`` for grant and decline alike
(auto-fkhq0.12, design checkpoint 2026-09-28). The token is minted when the
REQUESTING SESSION collects its result -- the bridge's result projector runs
only after ``status_for_principal`` authenticated that session -- on the
machine that accepted the request (its frozen ``result_destination_id``):

- Once per approval: the ``visitor_token_mints`` row commits with the guest
  (mission_control_db.create_visitor_token), and it outlives the guest, so a
  removed guest is never re-minted.
- Revealed once: the collecting read that mints carries the token; every
  later read carries the guest's participant id and ``delivered: true``. A
  response lost after the mint is not repeated: the operator sees the minted
  guest (and can remove it), and the requester asks again.
- Minted only while the canonical resolution is a Grant, re-read under the
  approval's lock at mint time.

Mirrors vault_open_central.py (planner, attention projection, HTTP adapter,
coordinator).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from collections.abc import Mapping
from typing import Any, Callable

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
    _ApprovalLocks,
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
from tools.dashboard.mailbox_central import _requesting_session
from tools.dashboard.vault_open_central import this_machine_label
from tools.graph import settings_ops
from tools.graph.schemas.central_attention import APPROVAL_REQUEST_SET_ID, ApprovalRequestV1

logger = logging.getLogger(__name__)

KIND = "visitor_token"
APPLICATION_SCOPE = "mission_control"
RENDERER_ID = "approval.visitor_token.review"
CONSUMER_ID = "visitor_token.local_mint.v1"
_DESTINATION_DOMAIN = b"dashboard.mission-control.visitor-mint-destination.v1"
_ATTENTION_DOMAIN = "dashboard.attention.visitor-token-recipient"
_REQUEST_FIELDS = {"display_name", "avatar", "reason"}

#: A person's name, as it will appear beside everything they ever ask.
_MAX_NAME = 120
_MAX_REASON = 500

#: Matches the route's own rule: the bytes arrive as a data URL and are put in
#: the attachment store once, never on the visitor row or the approval.
_AVATAR_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,")

#: Operator-facing states (review.application_result.state).
PENDING = "pending"
AWAITING = "awaiting_collection"
MINTED = "minted"
ELSEWHERE = "elsewhere"


def result_destination_id(secret: bytes | None = None) -> str:
    """This Dashboard's opaque identity as the machine that mints."""
    if secret is None:
        from tools.dashboard import unlock_routes
        secret = unlock_routes._session_secret()
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Dashboard session secret is unavailable")
    digest = hmac.new(secret, _DESTINATION_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def visitor_token_attention_id(approval_id: str) -> str:
    _bounded_approval_id(approval_id)
    return "attention-" + _opaque_digest([_ATTENTION_DOMAIN, 1, approval_id])


def _check_request(body: Any) -> tuple[str, str | None, str]:
    """Refuse at creation what could never be minted, with its reason."""
    if not isinstance(body, Mapping):
        raise ValueError("a visitor request is an object")
    unknown = set(body) - _REQUEST_FIELDS
    if unknown:
        raise ValueError(
            "a visitor request carries display_name, an optional avatar and an "
            f"optional reason; got also: {', '.join(sorted(unknown))}"
        )
    raw_name = body.get("display_name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not name:
        raise ValueError("display_name is required: it is how this person is "
                         "named beside everything they ask")
    if len(name) > _MAX_NAME:
        raise ValueError(f"display_name must be at most {_MAX_NAME} characters")
    avatar = body.get("avatar")
    if avatar in (None, ""):
        avatar = None
    elif not isinstance(avatar, str) or not _AVATAR_RE.match(avatar):
        raise ValueError("avatar must be a data:image/<png|jpeg|gif|webp>;base64 URL")
    raw_reason = body.get("reason")
    if raw_reason is not None and not isinstance(raw_reason, str):
        raise ValueError("reason is text")
    reason = (raw_reason or "").strip()
    if len(reason) > _MAX_REASON:
        raise ValueError(f"reason must be at most {_MAX_REASON} characters")
    return name, avatar, reason


def _store_avatar(avatar: str, name: str) -> str:
    from tools.dashboard.plugins.mission_control.entrypoints import api as mc
    attachment_id, error = mc._store_avatar(avatar, name)
    if error:
        raise ValueError(error)
    return attachment_id


def build_request_planner(
    *,
    destination_resolver: Callable[[], str] = result_destination_id,
    machine_label: Callable[[], str] = this_machine_label,
    store_avatar: Callable[[str, str], str] = _store_avatar,
):
    def plan(context: ApprovalPlanningContext, body: Mapping[str, Any]) -> ApprovalRequestPlan:
        name, avatar, reason = _check_request(body)
        session = _requesting_session(context)
        destination = destination_resolver()
        if not isinstance(destination, str) or len(destination) != 43:
            raise ValueError("this Dashboard cannot let anyone in right now")
        machine = machine_label()
        # The photo goes into the content-addressed attachment store HERE, so
        # the operator decides looking at a face. A declined request leaves
        # one de-duplicated copy of a photo that was already sent.
        attachment_id = store_avatar(avatar, name) if avatar else None
        label = context.requester_ref.get("label") or session
        return ApprovalRequestPlan(
            subject_ref=f"visitor:{context.approval_id}",
            safe_review={
                "title": "Let this person in?",
                "detail": f"{label} wants a link for {name} that opens your missions.",
                "requester_label": label,
                "display_name": name,
                "reason": reason,
                "avatar_url": f"/api/attachment/{attachment_id}" if attachment_id else None,
                "machine_label": machine,
            },
            request={"display_name": name, "reason": reason,
                     "avatar_attachment_id": attachment_id},
            staged={"result_destination_id": destination, "machine_label": machine},
        )

    return plan


def _validate_decision(_context, _request, decision, _is_grant) -> dict[str, Any]:
    # The token is minted after the Grant, for the requester only: grant and
    # decline carry nothing.
    if decision:
        raise ValueError("visitor_token decisions carry no payload")
    return {}


def build_approval_runtime(**planner_options) -> ApprovalKindRuntime:
    return ApprovalKindRuntime(
        request_planner=build_request_planner(**planner_options),
        decision_validator=_validate_decision,
        resolution_consumer_id=CONSUMER_ID,
        result_ref_builder=lambda approval_id, _request, _decision: f"visitor:{approval_id}",
    )


def build_attention_runtime(approvals: ApprovalService) -> AttentionPublicationRuntime:
    def plan(source: Any) -> AttentionProjectionPlan:
        if not isinstance(source, ApprovalStatus):
            raise ValueError("visitor_token projection requires approval status")
        request = source.request.payload
        if request.get("kind") != KIND:
            raise ValueError("visitor_token projection kind mismatch")
        resolution = source.resolution
        review = request.get("safe_review") or {}
        label = review.get("requester_label") or "A session"
        return AttentionProjectionPlan(
            attention_id=visitor_token_attention_id(source.request.approval_id),
            object_ref=source.request.approval_id,
            participant_role="recipient",
            attention_state="resolved" if resolution is not None else "needs_attention",
            safe_title="Let this person in?",
            safe_summary=f"{label} wants a link for {review.get('display_name') or '?'}"[:240],
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


def _mint(name: str, *, avatar_attachment_id: str | None, approval_id: str) -> dict | None:
    from tools.dashboard.dao import mission_control_db as db
    return db.create_visitor_token(name, avatar_attachment_id=avatar_attachment_id,
                                   approval_id=approval_id)


def _minted_for(approval_id: str) -> dict | None:
    from tools.dashboard.dao import mission_control_db as db
    return db.visitor_minted_for(approval_id)


class VisitorDesk:
    """The accepting machine's once-only mint of a granted guest, and its state."""

    def __init__(
        self,
        *,
        approvals: ApprovalService,
        destination_resolver: Callable[[], str] = result_destination_id,
        mint: Callable[..., dict | None] = _mint,
        minted_for: Callable[[str], dict | None] = _minted_for,
    ) -> None:
        self.approvals = approvals
        self._destination_resolver = destination_resolver
        self._mint = mint
        self._minted_for = minted_for

    def _here(self, payload: Mapping[str, Any]) -> bool:
        staged = payload.get("staged")
        destination = staged.get("result_destination_id") if isinstance(staged, Mapping) else None
        try:
            ours = self._destination_resolver()
        except Exception:
            return False
        return isinstance(destination, str) and hmac.compare_digest(destination, ours)

    def state(self, status: ApprovalStatus) -> str:
        payload = status.request.payload
        if payload.get("kind") != KIND:
            raise ValueError("not a visitor_token approval")
        here = self._here(payload)
        resolution = status.resolution
        if resolution is None:
            return PENDING if here else ELSEWHERE
        if resolution.payload.get("outcome") != "granted":
            return str(resolution.payload.get("outcome"))
        if not here:
            return ELSEWHERE
        return MINTED if self._minted_for(status.request.approval_id) is not None else AWAITING

    def operator_result(self, status: ApprovalStatus) -> dict:
        """review.application_result for the Central renderer. Never a token."""
        state = self.state(status)
        result: dict[str, Any] = {
            "state": state,
            "machine_label": (status.request.payload.get("staged") or {}).get("machine_label") or "",
        }
        if state == MINTED:
            guest = self._minted_for(status.request.approval_id) or {}
            result["participant_id"] = guest.get("participant_id")
            result["display_name"] = guest.get("display_name") or \
                (status.request.payload.get("request") or {}).get("display_name")
            result["removed"] = bool(guest.get("removed"))
        return result

    def collect(self, approval_id: str) -> dict | None:
        """The authenticated requester's result: the token on the read that
        mints it, the guest without it on every later read, null while
        nothing can be minted here.

        Called only from the bridge's result projector, after
        ``status_for_principal`` authenticated the requesting session."""
        with _ApprovalLocks.for_id(_bounded_approval_id(approval_id)):
            status = self.approvals.status(approval_id)
            payload = status.request.payload
            resolution = status.resolution
            if payload.get("kind") != KIND or resolution is None \
                    or resolution.payload.get("outcome") != "granted" or not self._here(payload):
                return None
            request = payload.get("request") or {}
            minted = self._mint(request["display_name"],
                                avatar_attachment_id=request.get("avatar_attachment_id"),
                                approval_id=approval_id)
            if minted is not None:
                logger.info("visitor_token: minted %s for %s", minted["participant_id"], approval_id)
                return {"approved": True, "execution": {
                    "ok": True,
                    "token": minted["token"],
                    "participant_id": minted["participant_id"],
                    "display_name": minted["display_name"],
                    "avatar_attachment_id": minted.get("avatar_attachment_id"),
                }}
            guest = self._minted_for(approval_id)
            if guest is None:
                raise RuntimeError("visitor mint neither committed nor recorded")
            execution = {"ok": True, "delivered": True,
                         "participant_id": guest["participant_id"],
                         "display_name": guest.get("display_name") or request["display_name"]}
            if guest.get("removed"):
                execution["removed"] = True
            return {"approved": True, "execution": execution}


def build_http_adapter(
    desk: VisitorDesk,
    *,
    reconcile: Callable[[str], ApprovalStatus | None] | None = None,
) -> ApprovalHttpKindAdapter:
    def project_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = payload.get("request")
        if not isinstance(request, Mapping):
            raise RuntimeError("visitor_token request is unavailable")
        return {"display_name": request.get("display_name"), "reason": request.get("reason")}

    def map_decision(body: Mapping[str, Any]) -> CanonicalLegacyDecision:
        if body == {"approved": False}:
            return CanonicalLegacyDecision("declined", {})
        raise ApprovalHttpBridgeError("invalid_decision")

    def project_result(status: ApprovalStatus) -> Mapping[str, Any] | None:
        if reconcile is not None:
            reconcile(status.request.approval_id)
        return desk.collect(status.request.approval_id)

    return ApprovalHttpKindAdapter(kind=KIND, request_projector=project_request,
                                   result_projector=project_result,
                                   legacy_decision_mapper=map_decision)


class VisitorCoordinator(DashboardAccessCoordinator):
    """The dashboard-access wake coordinator, publishing ``visitor_token``
    attention items. It never mints: only the requester's collect does."""

    def __init__(self, **kwargs) -> None:
        super().__init__(consumer=None, **kwargs)

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


__all__ = [
    "APPLICATION_SCOPE", "CONSUMER_ID", "KIND", "RENDERER_ID",
    "VisitorCoordinator", "VisitorDesk",
    "build_approval_runtime", "build_attention_runtime", "build_http_adapter",
    "result_destination_id", "visitor_token_attention_id",
]
