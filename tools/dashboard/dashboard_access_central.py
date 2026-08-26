"""Settings-native ``dashboard_access`` approval runtime and recovery.

The Central request and resolution are personal Settings truth.  The local
``dashboard_access_grants`` row is only the application result for the exact
Dashboard session realm that accepted the request, so it is materialized only
when the manifest-rooted session secret matches the frozen destination.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridgeError,
    ApprovalHttpKindAdapter,
    CanonicalLegacyDecision,
)
from tools.dashboard.approval_kind_registry import (
    ApprovalDecisionContext,
    ApprovalKindRuntime,
    ApprovalPlanningContext,
    ApprovalRequestPlan,
)
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    ApprovalStatus,
    resolve_personal_root_public_key,
)
from tools.dashboard.attention_index_service import (
    AttentionIndexError,
    AttentionIndexService,
)
from tools.dashboard.attention_registry import (
    AttentionProjectionPlan,
    AttentionPublicationRuntime,
    AttentionSourceEvidence,
    RegisteredAttentionProducer,
)
from tools.dashboard.dao import identity_sessions
from tools.graph import settings_ops
from tools.graph.schemas.central_attention import (
    APPROVAL_REQUEST_SET_ID,
    APPROVAL_RESOLUTION_SET_ID,
    CENTRAL_ATTENTION_REVISION,
    ApprovalRequestV1,
)
from tools.network.idkit.canonical import canonical_json
from tools.network.idkit.errors import IdkitError
from tools.network.idkit.keys import load_public_key, verify_signature


logger = logging.getLogger(__name__)

KIND = "dashboard_access"
APPLICATION_SCOPE = "sessions"
NOTIFICATION_CLASS = "approval.dashboard_access.requested"
RENDERER_ID = "approval.dashboard_access.review"
CONSUMER_ID = "dashboard_access.local_grant.v1"
GRANT_SCOPE = ["dashboard:ui"]
GRANT_TTL_SECONDS = 2 * 60 * 60
GRANT_SIGNING_DOMAIN = b"autonomy.identity.dashboard-access-grant.v1\n"
_DESTINATION_DOMAIN = b"dashboard.identity.access-result-destination.v1"
_ATTENTION_DOMAIN = "dashboard.attention.approval-recipient"
_CENTRAL_ID_PREFIX = "central-"
_MAX_PENDING_IDS = 256
_MIN_RETRY_SECONDS = 0.25
_MAX_RETRY_SECONDS = 30.0


def _opaque_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(encoded).digest()).decode(
        "ascii"
    ).rstrip("=")


def dashboard_access_attention_id(approval_id: str) -> str:
    _bounded_approval_id(approval_id)
    return "attention-" + _opaque_digest([_ATTENTION_DOMAIN, 1, approval_id])


def dashboard_access_result_destination_id(
    secret: bytes | None = None,
) -> str:
    if secret is None:
        # Runtime import avoids making unlock_routes' legacy compatibility
        # imports a module cycle during Dashboard startup.
        from tools.dashboard import unlock_routes

        secret = unlock_routes._session_secret()
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Dashboard session secret is unavailable")
    digest = hmac.new(secret, _DESTINATION_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _bounded_approval_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(_CENTRAL_ID_PREFIX)
        or value != value.strip()
    ):
        raise ValueError("invalid Central approval ID")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("invalid Central approval ID") from exc
    if not 1 <= len(encoded) <= 256 or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError("invalid Central approval ID")
    return value


def _valid_nonce(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _frozen_grant(
    context: ApprovalPlanningContext,
    ephemeral_pub: str,
    nonce: str,
) -> dict[str, Any]:
    issued_at = math.floor(context.planning_time)
    return {
        "v": 1,
        "nonce": nonce,
        "grantee": context.requester_ref["id"],
        "ephemeral_pub": ephemeral_pub,
        "scope": list(GRANT_SCOPE),
        "issued_at": issued_at,
        "expires_at": issued_at + GRANT_TTL_SECONDS,
    }


def build_request_planner(
    *,
    nonce_factory: Callable[[], str] | None = None,
    destination_resolver: Callable[[], str] = dashboard_access_result_destination_id,
):
    make_nonce = nonce_factory or (lambda: secrets.token_hex(32))

    def plan(
        context: ApprovalPlanningContext,
        body: Mapping[str, Any],
    ) -> ApprovalRequestPlan:
        if not isinstance(body, Mapping) or set(body) != {"ephemeral_pub"}:
            raise ValueError("dashboard access accepts only ephemeral_pub")
        ephemeral_pub = body.get("ephemeral_pub")
        try:
            load_public_key(ephemeral_pub)
        except (IdkitError, TypeError) as exc:
            raise ValueError("ephemeral_pub is not a valid Ed25519 key") from exc
        nonce = make_nonce()
        if not _valid_nonce(nonce):
            raise ValueError("dashboard access nonce generator failed")
        destination_id = destination_resolver()
        if not isinstance(destination_id, str) or len(destination_id) != 43:
            raise ValueError("dashboard result destination is unavailable")
        grant = _frozen_grant(context, ephemeral_pub, nonce)
        label = context.requester_ref.get("label") or "Authenticated session"
        safe_review = {
            "title": "Dashboard access requested",
            "detail": f"{label} wants temporary access to this Dashboard.",
            "requester_label": label,
            "scope_label": "Dashboard UI",
            "expires_at": grant["expires_at"],
            "grant": grant,
        }
        return ApprovalRequestPlan(
            subject_ref=f"dashboard-access:{nonce}",
            safe_review=safe_review,
            request={"ephemeral_pub": ephemeral_pub},
            staged={
                "grant": grant,
                "result_destination_id": destination_id,
            },
        )

    return plan


def _validate_grant_decision(
    context: ApprovalDecisionContext,
    request_payload: Mapping[str, Any],
    decision: Mapping[str, Any],
    is_grant: bool,
    *,
    root_resolver: Callable[[], str] = resolve_personal_root_public_key,
) -> dict[str, Any]:
    if not is_grant:
        if decision:
            raise ValueError("decline carries no decision payload")
        return {}
    if not isinstance(decision, Mapping) or set(decision) != {"grant", "signature"}:
        raise ValueError("grant requires the frozen grant and signature")
    staged = request_payload.get("staged")
    expected = staged.get("grant") if isinstance(staged, Mapping) else None
    grant = decision.get("grant")
    signature = decision.get("signature")
    if not isinstance(expected, Mapping) or not isinstance(grant, Mapping):
        raise ValueError("frozen grant is unavailable")
    if canonical_json(dict(grant)) != canonical_json(dict(expected)):
        raise ValueError("signed grant differs from frozen truth")
    if (
        set(grant) != {
            "v", "nonce", "grantee", "ephemeral_pub", "scope",
            "issued_at", "expires_at",
        }
        or grant.get("v") != 1
        or not _valid_nonce(grant.get("nonce"))
        or grant.get("scope") != GRANT_SCOPE
        or isinstance(grant.get("issued_at"), bool)
        or not isinstance(grant.get("issued_at"), int)
        or isinstance(grant.get("expires_at"), bool)
        or not isinstance(grant.get("expires_at"), int)
        or grant["expires_at"] - grant["issued_at"] != GRANT_TTL_SECONDS
        or grant.get("ephemeral_pub") != request_payload.get("request", {}).get(
            "ephemeral_pub"
        )
        or not isinstance(signature, str)
    ):
        raise ValueError("frozen grant is malformed")
    requester = request_payload.get("requester_ref")
    if not isinstance(requester, Mapping) or grant.get("grantee") != requester.get("id"):
        raise ValueError("grant requester binding differs")
    request_deadline = request_payload.get("expires_at")
    decision_time = context.decision_time
    if (
        isinstance(decision_time, bool)
        or not isinstance(decision_time, (int, float))
        or not math.isfinite(float(decision_time))
        or not isinstance(request_deadline, (int, float))
        or decision_time >= float(request_deadline)
        or decision_time >= grant["expires_at"]
    ):
        raise ValueError("dashboard access grant has expired")
    verify_signature(
        root_resolver(),
        signature,
        GRANT_SIGNING_DOMAIN + canonical_json(dict(grant)),
    )
    return {"grant": dict(grant), "signature": signature}


def build_approval_runtime(
    *,
    nonce_factory: Callable[[], str] | None = None,
    destination_resolver: Callable[[], str] = dashboard_access_result_destination_id,
    root_resolver: Callable[[], str] = resolve_personal_root_public_key,
) -> ApprovalKindRuntime:
    planner = build_request_planner(
        nonce_factory=nonce_factory,
        destination_resolver=destination_resolver,
    )

    def validate(context, request, decision, is_grant):
        return _validate_grant_decision(
            context,
            request,
            decision,
            is_grant,
            root_resolver=root_resolver,
        )

    return ApprovalKindRuntime(
        request_planner=planner,
        decision_validator=validate,
        resolution_consumer_id=CONSUMER_ID,
        result_ref_builder=lambda approval_id, _request, _decision: (
            f"dashboard-access:{approval_id}"
        ),
    )


def build_attention_runtime(
    approvals: ApprovalService,
) -> AttentionPublicationRuntime:
    def plan(source: Any) -> AttentionProjectionPlan:
        if not isinstance(source, ApprovalStatus):
            raise ValueError("dashboard access projection requires approval status")
        request = source.request.payload
        if request.get("kind") != KIND:
            raise ValueError("dashboard access projection kind mismatch")
        resolution = source.resolution
        version = 2 if resolution is not None else 1
        requester = request.get("requester_ref")
        label = (
            requester.get("label")
            if isinstance(requester, Mapping) and isinstance(requester.get("label"), str)
            else "Authenticated session"
        )
        return AttentionProjectionPlan(
            attention_id=dashboard_access_attention_id(source.request.approval_id),
            object_ref=source.request.approval_id,
            participant_role="recipient",
            attention_state="resolved" if resolution is not None else "needs_attention",
            safe_title="Dashboard access requested",
            safe_summary=f"{label} wants temporary Dashboard access.",
            counterparty_ref=None,
            occurred_at=(
                float(resolution.payload["resolved_at"])
                if resolution is not None
                else float(request["created_at"])
            ),
            source_version=version,
        )

    def evidence(object_ref: str, source_version: int) -> AttentionSourceEvidence:
        status = approvals.status(_bounded_approval_id(object_ref))
        actual = 2 if status.resolution is not None else 1
        if actual != source_version or status.request.payload.get("kind") != KIND:
            raise AttentionIndexError("stale_source")
        return AttentionSourceEvidence(
            source_guard={
                "kind": "approval",
                "ref": object_ref,
                "version": source_version,
            },
            source_expires_at=status.request.payload.get("expires_at"),
        )

    return AttentionPublicationRuntime(
        projection_planner=plan,
        source_evidence_builder=evidence,
    )


class DashboardAccessResultConsumer:
    def __init__(
        self,
        *,
        root_resolver: Callable[[], str] = resolve_personal_root_public_key,
        destination_resolver: Callable[[], str] = dashboard_access_result_destination_id,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root_resolver = root_resolver
        self._destination_resolver = destination_resolver
        self._clock = clock

    @staticmethod
    def _matching_local_row(
        row: Mapping[str, Any],
        *,
        approval_id: str,
        grant: Mapping[str, Any],
        signature: str,
    ) -> bool:
        return all((
            row.get("approval_id") == approval_id,
            row.get("ephemeral_pub") == grant.get("ephemeral_pub"),
            row.get("operator_signature") == signature,
            row.get("grantee") == grant.get("grantee"),
            row.get("scope") == grant.get("scope"),
            row.get("issued_at") == grant.get("issued_at"),
            row.get("expires_at") == grant.get("expires_at"),
        ))

    def materialize(self, status: ApprovalStatus) -> bool:
        request = status.request.payload
        resolution = status.resolution
        if request.get("kind") != KIND or resolution is None:
            return False
        if resolution.payload.get("outcome") != "granted":
            return False
        staged = request.get("staged")
        if not isinstance(staged, Mapping):
            raise RuntimeError("dashboard access staging is unavailable")
        destination = staged.get("result_destination_id")
        if not isinstance(destination, str) or not hmac.compare_digest(
            destination,
            self._destination_resolver(),
        ):
            return False
        decision = resolution.payload.get("decision")
        if not isinstance(decision, Mapping):
            raise RuntimeError("dashboard access decision is unavailable")
        validated = _validate_grant_decision(
            ApprovalDecisionContext(
                approval_id=status.request.approval_id,
                decision_time=float(resolution.payload["resolved_at"]),
            ),
            request,
            decision,
            True,
            root_resolver=self._root_resolver,
        )
        grant = validated["grant"]
        signature = validated["signature"]
        existing = identity_sessions.get_access_grant(grant["nonce"])
        if existing is not None:
            if not self._matching_local_row(
                existing,
                approval_id=status.request.approval_id,
                grant=grant,
                signature=signature,
            ):
                raise RuntimeError("dashboard access application result conflicts")
            return True
        try:
            now = float(self._clock())
        except (OverflowError, TypeError, ValueError) as exc:
            raise RuntimeError("dashboard access application result unavailable") from exc
        if not math.isfinite(now):
            raise RuntimeError("dashboard access application result unavailable")
        # A grant that was materialized while live remains an idempotent local
        # application result after its redemption window closes.  A missing
        # row must never be created at or after that deadline: Central truth
        # proves the operator's timely decision, not timely local execution.
        if now >= grant["expires_at"]:
            return False
        identity_sessions.store_access_grant(
            nonce=grant["nonce"],
            approval_id=status.request.approval_id,
            ephemeral_pub=grant["ephemeral_pub"],
            operator_signature=signature,
            grantee=grant["grantee"],
            scope=grant["scope"],
            issued_at=grant["issued_at"],
            expires_at=grant["expires_at"],
            approved_at=float(resolution.payload["resolved_at"]),
        )
        row = identity_sessions.get_access_grant(grant["nonce"])
        if row is None or not self._matching_local_row(
            row,
            approval_id=status.request.approval_id,
            grant=grant,
            signature=signature,
        ):
            raise RuntimeError("dashboard access application result conflicts")
        return True

    def project(self, status: ApprovalStatus) -> Mapping[str, Any] | None:
        resolution = status.resolution
        if resolution is None or resolution.payload.get("outcome") != "granted":
            return None
        if not self.materialize(status):
            return None
        decision = resolution.payload.get("decision")
        if not isinstance(decision, Mapping):
            raise RuntimeError("dashboard access decision is unavailable")
        return {
            "approved": True,
            "grant": dict(decision["grant"]),
            "signature": decision["signature"],
            "execution": {"ok": True},
        }


def build_http_adapter(
    consumer: DashboardAccessResultConsumer,
    *,
    reconcile: Callable[[str], ApprovalStatus | None] | None = None,
) -> ApprovalHttpKindAdapter:
    def project_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = payload.get("request")
        if not isinstance(request, Mapping) or set(request) != {"ephemeral_pub"}:
            raise RuntimeError("dashboard access request is unavailable")
        return {"ephemeral_pub": request["ephemeral_pub"]}

    def map_decision(body: Mapping[str, Any]) -> CanonicalLegacyDecision:
        if body == {"approved": False}:
            return CanonicalLegacyDecision("declined", {})
        if (
            isinstance(body, Mapping)
            and set(body) == {"approved", "grant", "signature"}
            and body.get("approved") is True
        ):
            return CanonicalLegacyDecision(
                "granted",
                {"grant": body["grant"], "signature": body["signature"]},
            )
        raise ApprovalHttpBridgeError("invalid_decision")

    def project_result(status: ApprovalStatus) -> Mapping[str, Any] | None:
        if reconcile is not None:
            refreshed = reconcile(status.request.approval_id)
            if refreshed is not None:
                status = refreshed
        return consumer.project(status)

    return ApprovalHttpKindAdapter(
        kind=KIND,
        request_projector=project_request,
        result_projector=project_result,
        legacy_decision_mapper=map_decision,
    )


@dataclass(frozen=True, slots=True)
class SyncedSettingsAddress:
    set_id: str
    schema_revision: int
    key: str


class DashboardAccessCoordinator:
    """Bounded wake coordinator; durable Settings remain the only queue."""

    def __init__(
        self,
        *,
        approvals: ApprovalService,
        index: AttentionIndexService,
        producer: RegisteredAttentionProducer,
        consumer: DashboardAccessResultConsumer,
    ) -> None:
        self.approvals = approvals
        self.index = index
        self.producer = producer
        self.consumer = consumer
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._full_scan_due = False
        self._scheduled = False
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._retry_seconds = _MIN_RETRY_SECONDS

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is not None and self._loop is not loop:
                raise RuntimeError("dashboard access coordinator loop changed")
            self._loop = loop
            self._stopping = False
            self._full_scan_due = True
            self._retry_seconds = _MIN_RETRY_SECONDS
            self._schedule_locked(loop)

    async def stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._loop = None
            self._pending.clear()
            self._full_scan_due = False
            self._scheduled = False
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _schedule_locked(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._scheduled:
            return
        self._scheduled = True
        try:
            loop.call_soon_threadsafe(self._begin_drain)
        except RuntimeError:
            self._scheduled = False

    def offer(self, approval_id: Any) -> None:
        try:
            bounded = _bounded_approval_id(approval_id)
        except ValueError:
            return
        with self._lock:
            loop = self._loop
            if loop is None or loop.is_closed() or self._stopping:
                return
            if bounded not in self._pending and len(self._pending) >= _MAX_PENDING_IDS:
                self._pending.clear()
                self._full_scan_due = True
            else:
                self._pending.add(bounded)
            self._schedule_locked(loop)

    def offer_gap(self) -> None:
        with self._lock:
            loop = self._loop
            if loop is None or loop.is_closed() or self._stopping:
                return
            self._full_scan_due = True
            self._schedule_locked(loop)

    def offer_local_setting(
        self,
        *,
        operation: Any,
        snapshot: Any,
        org: Any,
    ) -> None:
        try:
            if org is not None or not isinstance(snapshot, Mapping):
                return
            if snapshot.get("set_id") not in {
                APPROVAL_REQUEST_SET_ID,
                APPROVAL_RESOLUTION_SET_ID,
            }:
                return
            revision = snapshot.get("schema_revision")
            if isinstance(revision, bool) or revision != CENTRAL_ATTENTION_REVISION:
                return
            if not isinstance(operation, str) or not operation:
                return
            self.offer(snapshot.get("key"))
        except Exception:
            self.offer_gap()

    def offer_synced(
        self,
        *,
        addresses: Iterable[Any] = (),
        gap: bool = False,
    ) -> None:
        try:
            if gap:
                self.offer_gap()
            for address in addresses:
                set_id = getattr(address, "set_id", None)
                revision = getattr(address, "schema_revision", None)
                key = getattr(address, "key", None)
                if (
                    set_id in {
                        APPROVAL_REQUEST_SET_ID,
                        APPROVAL_RESOLUTION_SET_ID,
                    }
                    and not isinstance(revision, bool)
                    and revision == CENTRAL_ATTENTION_REVISION
                ):
                    self.offer(key)
        except Exception:
            self.offer_gap()

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
        rows = settings_ops.read_set(
            APPROVAL_REQUEST_SET_ID,
            org=None,
            peers=[],
        )
        if any(rows.dropped.values()):
            raise RuntimeError("partial Central approval request read")
        selected: list[str] = []
        for row in rows:
            if not isinstance(row.payload, dict):
                raise RuntimeError("invalid Central approval request row")
            ApprovalRequestV1.validate(row.payload)
            if row.payload.get("kind") == KIND:
                selected.append(_bounded_approval_id(row.key))
        return tuple(sorted(set(selected)))

    def _begin_drain(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._stopping or self._loop is None:
                        self._scheduled = False
                        return
                    pending = tuple(sorted(self._pending))
                    self._pending.clear()
                    scan = self._full_scan_due
                    self._full_scan_due = False
                try:
                    for approval_id in pending:
                        await asyncio.to_thread(self.reconcile_exact, approval_id)
                    if scan:
                        selected = await asyncio.to_thread(self._scan_ids)
                        for approval_id in selected:
                            await asyncio.to_thread(self.reconcile_exact, approval_id)
                    self._retry_seconds = _MIN_RETRY_SECONDS
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("dashboard access reconciliation failed")
                    with self._lock:
                        if self._loop is not None and not self._stopping:
                            self._full_scan_due = True
                    await asyncio.sleep(self._retry_seconds)
                    self._retry_seconds = min(
                        _MAX_RETRY_SECONDS,
                        self._retry_seconds * 2,
                    )
                with self._lock:
                    if not self._pending and not self._full_scan_due:
                        self._scheduled = False
                        return
        finally:
            with self._lock:
                if self._loop is None or self._stopping:
                    self._scheduled = False


__all__ = [
    "APPLICATION_SCOPE",
    "CONSUMER_ID",
    "DashboardAccessCoordinator",
    "DashboardAccessResultConsumer",
    "KIND",
    "NOTIFICATION_CLASS",
    "RENDERER_ID",
    "build_approval_runtime",
    "build_attention_runtime",
    "build_http_adapter",
    "dashboard_access_attention_id",
    "dashboard_access_result_destination_id",
]
