"""``fleet_machine_admission`` on the current generic approval rendezvous.

Fleet owns transport, signed roster evidence, and delivery state.  The generic
approval surface owns the human decision.  The stable kind and source approval
id are deliberately independent of today's SQLite rendezvous so the same
dialogue/executor can later move to Settings without changing Fleet state.
"""

from __future__ import annotations

import re
import time

from starlette.requests import Request

from tools.dashboard import identity_routes, unlock_routes
from tools.dashboard.dao import approval_requests as ar
from tools.dashboard.event_bus import event_bus
from tools.network import fleet_enroll, fleet_roster


KIND = "fleet_machine_admission"
_APPROVAL_PREFIX = "fleet-"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def approval_id_for(request_id: str) -> str:
    if not isinstance(request_id, str) or not _HEX64.fullmatch(request_id):
        raise ValueError("fleet source request id must be 64 lowercase hex chars")
    return _APPROVAL_PREFIX + request_id


def prepare_create(_session: str, _request: dict) -> tuple[dict, dict]:
    """External callers may not manufacture Fleet admission requests."""
    raise ValueError("fleet admission approvals are created by the invitation channel")


def ensure_approval(pending, *, store, db_path=None) -> str:
    """Create/bind the one deterministic approval for a validated request."""
    approval_id = approval_id_for(pending.request_id)
    safe_request = {
        "source_request_id": pending.request_id,
        "target_uuid": pending.target_uuid,
        "verification_code": pending.verification_code,
    }
    staged = {
        "v": 1,
        "source_request_id": pending.request_id,
        "target_uuid": pending.target_uuid,
        "request": pending.request.to_dict(),
        "channel_binding": pending.channel_binding,
        "verification_code": pending.verification_code,
        "personal_root_pub": pending.request.personal_root_pub,
        "issued_at": pending.created_at,
    }
    created = ar.create_idempotent(
        request_id=approval_id,
        kind=KIND,
        session=f"fleet:{pending.target_uuid}",
        request=safe_request,
        staged=staged,
        created_at=pending.created_at / 1000,
        db_path=db_path,
    )
    store.bind_approval(pending.request_id, approval_id)
    if created:
        event_bus.broadcast_sync(
            "approval:pending",
            {"id": approval_id, "kind": KIND, "session": f"fleet:{pending.target_uuid}"},
            dedup=False,
        )
    return approval_id


def enrich(row: dict) -> dict:
    staged = row.get("staged")
    if not isinstance(staged, dict):
        return {"staged": None, "application": "fleet"}
    return {"staged": staged, "application": "fleet"}


def decision_status(approval_id: str | None, *, db_path=None) -> str | None:
    """Project only the human verdict; never copy it into Fleet storage."""
    if not approval_id:
        return None
    row = ar.get(approval_id, db_path=db_path)
    result = (row or {}).get("result")
    if not isinstance(result, dict):
        return None
    return "granted" if result.get("approved") is True else "declined"


def terminal_approval_ids(*, db_path=None) -> set[str]:
    return ar.decided_ids_for_kind(KIND, db_path=db_path)


def authorize_decision(request: Request, row: dict, decision: dict) -> str | None:
    session = unlock_routes.session_from_request(request)
    if session is None:
        return "unlock the dashboard before deciding this machine admission"
    if decision.get("approved"):
        if set(decision) != {"approved", "approval", "roster_entry"}:
            return "fleet approval must carry only its two signed evidence records"
    elif set(decision) != {"approved"}:
        return "fleet decline must carry only the decision"
    staged = row.get("staged")
    if not isinstance(staged, dict) or staged.get("v") != 1:
        return "fleet admission has no server-frozen request"
    return None


def _anchor() -> str:
    personal = identity_routes._personal_member()
    anchor = (personal.payload if personal is not None else {}).get("root_pub")
    if not isinstance(anchor, str) or not _HEX64.fullmatch(anchor):
        raise ValueError("stored personal identity has no public root anchor")
    return anchor


async def execute(row: dict, decision: dict) -> dict:
    from tools.dashboard import fleet_enrollment_service

    staged = row.get("staged") or {}
    target_uuid = staged.get("target_uuid")
    request_id = staged.get("source_request_id")
    store = fleet_enrollment_service.FleetEnrollmentStore()
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
        approved = store.approve(
            target_uuid=target_uuid,
            request_id=request_id,
            approval=approval,
            roster_entry=roster_entry,
            anchor_root_pub=anchor,
            org=None,
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


PREPARE_CREATE = {KIND: prepare_create}
ENRICH = {KIND: enrich}
EXECUTORS = {KIND: execute}
AUTHORIZE_DECISION = {KIND: authorize_decision}
