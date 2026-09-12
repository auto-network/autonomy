"""Settings-native Fleet admission. Runtime private keys are not decision fields."""
from __future__ import annotations
import os
import re
import time
from tools.dashboard import identity_routes
from tools.dashboard.approval_http_bridge import central_stable_approval_id
from tools.dashboard.approval_kind_registry import ApprovalKindRuntime, ApprovalRequestPlan, RegisteredApprovalProducer
from tools.dashboard.approval_service import ApprovalServiceError, normalize_unix_milliseconds_deadline
from tools.dashboard.attention_registry import AttentionProjectionPlan, AttentionPublicationRuntime, AttentionSourceEvidence
from tools.network import fleet_enroll, fleet_machine_profile, fleet_roster, machine_boot

KIND = "fleet_machine_admission"
APPLICATION_SCOPE = "fleet"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
PRODUCER = RegisteredApprovalProducer("fleet.machine_admission", "Fleet enrollment", frozenset({KIND}), True)


def _runtime():
    from tools.dashboard.attention_routes import approval_runtime
    return approval_runtime()


def _store():
    from tools.dashboard.fleet_enrollment_service import FleetEnrollmentStore
    return FleetEnrollmentStore()


def _needs_local_bootstrap():
    return machine_boot.machine_id(org="machine") is None and not fleet_roster.load_entries(org=None)


def approval_id_for(request_id):
    if not isinstance(request_id, str) or not _HEX64.fullmatch(request_id):
        raise ValueError("invalid Fleet request ID")
    return central_stable_approval_id(KIND, request_id)


def build_approval_runtime():
    def plan(context, body):
        if set(body) != {"request_id"}:
            raise ValueError("Fleet producer accepts only request_id")
        store = _store()
        pending = store.get_request(body["request_id"])
        if pending is None or context.approval_id != approval_id_for(pending.request_id):
            raise ValueError("Fleet approval correlation mismatch")
        existing = _runtime().approvals.store.get_request(context.approval_id)
        if existing is not None:
            payload = existing.payload
            return ApprovalRequestPlan(
                subject_ref=payload["subject_ref"], request=payload["request"],
                staged=payload["staged"], safe_review=payload["safe_review"],
                trusted_source_expires_at=payload.get("expires_at"),
            )
        with store._connect() as conn:
            invite = store._invite_row(conn, pending.target_uuid, int(context.planning_time * 1000))
        fleet_enroll.verify_request(pending.request, invite=invite)
        staged = {
            "v": 1, "source_request_id": pending.request_id,
            "target_uuid": pending.target_uuid, "request": pending.request.to_dict(),
            "channel_binding": pending.channel_binding, "verification_code": pending.verification_code,
            "personal_root_pub": pending.request.personal_root_pub, "issued_at": pending.created_at,
            "local_bootstrap_machine_id": os.urandom(32).hex() if _needs_local_bootstrap() else None,
        }
        return ApprovalRequestPlan(
            subject_ref="fleet-enrollment:" + pending.request_id,
            request={"source_request_id": pending.request_id}, staged=staged,
            safe_review={"title": "Add this machine?", "detail": "Compare the code with the one on the new machine.",
                         "verification_code": pending.verification_code, "fleet": staged},
            trusted_source_expires_at=normalize_unix_milliseconds_deadline(invite.expires_at),
        )

    def validate(context, payload, decision, is_grant):
        if not is_grant:
            if decision:
                raise ValueError("Decline has no payload")
            return {}
        staged = payload["staged"]
        expected = {"machine_name", "approval", "roster_entry"}
        if staged["local_bootstrap_machine_id"]:
            expected.add("local_roster_entry")
        if set(decision) != expected:
            raise ValueError("Fleet decisions contain only public admission evidence")
        name = fleet_machine_profile.normalize_display_name(decision["machine_name"])
        fleet_enroll.verify_admission_evidence(
            fleet_enroll.EnrollmentApproval.from_dict(decision["approval"]),
            fleet_enroll.EnrollmentRequest.from_dict(staged["request"]),
            channel_binding=staged["channel_binding"],
            roster_entry=fleet_roster.RosterEntry.from_dict(decision["roster_entry"]),
            anchor_root_pub=_anchor(),
        )
        if staged["local_bootstrap_machine_id"]:
            local = fleet_roster.RosterEntry.from_dict(decision["local_roster_entry"])
            fleet_roster.verify(local, anchor_root_pub=_anchor())
            if (local.machine_id != staged["local_bootstrap_machine_id"]
                    or local.kind != fleet_roster.EntryKind.ENROLL
                    or local.assignment != fleet_roster.FLEET_MEMBER_ASSIGNMENT):
                raise ValueError("Local bootstrap evidence mismatch")
        return {**decision, "machine_name": name}

    return ApprovalKindRuntime(
        request_planner=plan, decision_validator=validate, resolution_consumer_id="fleet.machine_admission",
        result_ref_builder=lambda _id, payload, _decision: payload["subject_ref"],
    )


def build_attention_runtime(approvals):
    def plan(status):
        payload, resolution = status.request.payload, status.resolution
        return AttentionProjectionPlan(
            attention_id=status.request.approval_id, object_ref=status.request.approval_id,
            participant_role="recipient", attention_state="resolved" if resolution else "needs_attention",
            safe_title="Add this machine?", safe_summary=payload["safe_review"]["detail"],
            counterparty_ref=None, occurred_at=resolution.payload["resolved_at"] if resolution else payload["created_at"],
            source_version=2 if resolution else 1,
        )
    def evidence(ref, version):
        status = approvals.status(ref)
        if status.request.payload["kind"] != KIND or version != (2 if status.resolution else 1):
            raise ValueError("Fleet attention source mismatch")
        return AttentionSourceEvidence(
            source_guard={"kind": "approval", "ref": ref, "version": version},
            source_expires_at=status.request.payload.get("expires_at"),
        )
    return AttentionPublicationRuntime(projection_planner=plan, source_evidence_builder=evidence)


def ensure_approval(pending, *, store):
    runtime = _runtime()
    approval_id = approval_id_for(pending.request_id)
    runtime.approvals.create_from_producer(
        KIND, PRODUCER, {"request_id": pending.request_id}, source_approval_id=approval_id,
    )
    store.bind_approval(pending.request_id, approval_id)
    reconcile(approval_id)
    return approval_id


def decision_status(approval_id):
    if not approval_id:
        return None
    try:
        status = _runtime().approvals.status(approval_id)
    except ApprovalServiceError as exc:
        if exc.code == "not_found":
            return None
        raise
    return status.resolution.payload["outcome"] if status.resolution else "open"


def terminal_approval_ids(approval_ids):
    return {approval_id for approval_id in approval_ids
            if decision_status(approval_id) in {"granted", "declined", "canceled", "expired"}}


def reconcile(approval_id):
    runtime = _runtime()
    status = runtime.approvals.status(approval_id)
    if status.request.payload["kind"] != KIND:
        return
    runtime.index.publish(runtime.index.registry.producer(KIND, APPLICATION_SCOPE), status)
    if status.resolution and status.resolution.payload["outcome"] == "granted":
        pending = _store().get_request(status.request.payload["staged"]["source_request_id"])
        if pending and pending.source_approval_id == approval_id and pending.status in {"pending", "approving"}:
            execute({"id": approval_id, "staged": status.request.payload["staged"]},
                    status.resolution.payload["decision"])


def project_result(status):
    if not status.resolution or status.resolution.payload["outcome"] != "granted":
        return None
    pending = _store().get_request(status.request.payload["staged"]["source_request_id"])
    if pending is None or pending.status in {"pending", "approving"}:
        return None
    return {"execution": {"ok": pending.status == "approved",
                          "error": pending.last_error_code if pending.status != "approved" else None}}


def _anchor() -> str:
    personal = identity_routes._personal_member()
    anchor = (personal.payload if personal is not None else {}).get("root_pub")
    if not isinstance(anchor, str) or not _HEX64.fullmatch(anchor):
        raise ValueError("stored personal identity has no public root anchor")
    return anchor


def execute(row: dict, decision: dict) -> dict:
    staged = row.get("staged") or {}
    target_uuid = staged.get("target_uuid")
    request_id = staged.get("source_request_id")
    store = _store()
    try:
        pending = store.get_request(request_id)
        if pending is None:
            raise ValueError("fleet enrollment request no longer exists")
        if pending.source_approval_id != row.get("id"):
            raise ValueError("fleet request is not bound to this approval")
        if pending.request.to_dict() != staged.get("request"):
            raise ValueError("fleet request changed after approval was staged")
        if pending.channel_binding != staged.get("channel_binding"):
            raise ValueError("fleet channel changed after approval was staged")
        if pending.verification_code != staged.get("verification_code"):
            raise ValueError("fleet comparison code changed after approval was staged")
        anchor = _anchor()
        if staged.get("personal_root_pub") != anchor:
            raise ValueError("fleet admission is not anchored to the stored personal root")
        approval = fleet_enroll.EnrollmentApproval.from_dict(decision.get("approval"))
        roster_entry = fleet_roster.RosterEntry.from_dict(decision.get("roster_entry"))
        # On a standalone dashboard, validate the remote authorization before
        # the first local Fleet row is allowed to exist. ``approve`` repeats
        # this check at commit time; the preflight prevents invalid joiner
        # evidence from partially bootstrapping the origin Dashboard.
        store.verify_pending_approval_evidence(
            target_uuid=target_uuid,
            request_id=request_id,
            approval=approval,
            roster_entry=roster_entry,
            anchor_root_pub=anchor,
        )
        bootstrap_id = staged.get("local_bootstrap_machine_id")
        if bootstrap_id:
            local_entry = fleet_roster.RosterEntry.from_dict(
                decision.get("local_roster_entry")
            )
            if local_entry.machine_id != bootstrap_id:
                raise ValueError("local Fleet bootstrap changed machine id")
            machine_boot.accept_local_bootstrap(
                local_entry,
                anchor_root_pub=anchor,
                org="machine",
            )
        approved = store.approve(
            target_uuid=target_uuid,
            request_id=request_id,
            approval=approval,
            roster_entry=roster_entry,
            anchor_root_pub=anchor,
            org=None,
        )
        fleet_machine_profile.store(
            roster_entry.machine_id,
            decision.get("machine_name"),
        )
        return {
            "ok": True,
            "request_id": approved.request_id,
            "roster_entry_id": roster_entry.entry_id,
        }
    except Exception as exc:
        try:
            store.fail(
                target_uuid=target_uuid,
                request_id=request_id,
                error_code="approval_execution_failed",
                now_ms=int(time.time() * 1000),
            )
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
