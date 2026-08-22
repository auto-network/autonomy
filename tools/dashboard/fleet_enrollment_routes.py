"""Operator Dashboard API for machine-neutral fleet enrollment.

The browser owns every personal-root signature.  These routes register its
signed invitation, display pending public requests, and verify the two signed
approval records.  They never accept a root seed or signing key.  Approval and
decline require the human Dashboard session cookie; a session bearer can
authenticate an API caller but cannot exercise personal-root authority.
"""

from __future__ import annotations

import sqlite3
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import (
    fleet_enrollment_service,
    identity_routes,
    link_serving,
    unlock_routes,
)
from tools.graph.db import _org_db_path
from tools.network import (
    fleet_invite,
    fleet_relay_sync,
    fleet_runtime,
    fleet_roster,
    fleet_sync_scheduler,
    fleet_tunnel_server,
    machine_boot,
)
from tools.network.fleet_enrollment_client import FleetJoinStateStore


def _operator_required(request: Request) -> JSONResponse | None:
    if unlock_routes.session_from_request(request) is None:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "fleet enrollment approval requires an unlocked human "
                    "Dashboard session"
                ),
            },
            status_code=401,
        )
    return None


async def register_invite(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    expected = {"org", "target_uuid", "grant_token", "invite"}
    if not isinstance(body, dict) or set(body) != expected:
        return JSONResponse(
            {"ok": False, "error": f"body must carry exactly {sorted(expected)}"},
            status_code=400,
        )
    org = body["org"]
    token = body["grant_token"]
    if not isinstance(org, str) or not org:
        return JSONResponse({"ok": False, "error": "org must be a non-empty slug"}, status_code=400)
    try:
        invite = fleet_invite.FleetInvite.from_dict(body["invite"])
        fleet_invite.verify(invite)
        personal = identity_routes._personal_member()
        anchor = (personal.payload if personal is not None else {}).get("root_pub")
        if not isinstance(anchor, str) or invite.personal_root_pub != anchor:
            raise ValueError("invite is not anchored to the stored personal identity")
        grant = link_serving.check_grant(token, org=org, now=time.time())
        if (
            grant is None
            or grant.get("target_type") != "fleet:join"
            or grant.get("target_uuid") != body["target_uuid"]
        ):
            raise ValueError("grant is unavailable or is not this fleet invitation")
        fleet_enrollment_service.FleetEnrollmentStore().register_invite(
            target_uuid=body["target_uuid"],
            grant_token=token,
            invite=invite,
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({
        "ok": True,
        "target_uuid": body["target_uuid"],
        "invite_id": invite.invite_id,
        "rendezvous": invite.rendezvous,
    })


async def pending_requests(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    target_uuid = request.query_params.get("target_uuid")
    try:
        rows = fleet_enrollment_service.FleetEnrollmentStore().list_pending(
            target_uuid
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({
        "ok": True,
        "requests": [
            {
                "request_id": row.request_id,
                "target_uuid": row.target_uuid,
                "request": row.request.to_dict(),
                "channel_binding": row.channel_binding,
                "verification_code": row.verification_code,
                "status": row.status,
                "source_approval_id": row.source_approval_id,
                "last_error_code": row.last_error_code,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ],
    })


def _local_completion_state():
    state = FleetJoinStateStore()
    recovery = state.latest_any()
    if recovery is None:
        return state, None, None
    return state, recovery, state.load_delivery(recovery.request_id)


def _runtime_context() -> tuple[str, fleet_roster.RosterEntry] | None:
    machine_id_value = machine_boot.machine_id(org="machine")
    root_pub = fleet_tunnel_server._personal_root_pub()
    if machine_id_value is None or root_pub is None:
        return None
    active = fleet_roster.resolve(
        fleet_roster.load_entries(org=None), anchor_root_pub=root_pub
    )
    entry = next(
        (item for item in active.values() if item.machine_id == machine_id_value),
        None,
    )
    if entry is None:
        return None
    return root_pub, entry


def _ensure_fleet_catalog(machine_pub: str) -> None:
    """Activate authored personal-DB capture under the durable machine id."""
    from tools.graph.db import GraphDB

    path = _org_db_path("personal")
    GraphDB.close_pooled_path(path)
    db = GraphDB(path, attach_fleet_sync=False)
    try:
        db.migrate_fleet_sync_catalog(machine_pub)
        db.activate_fleet_sync_writers(machine_pub)
    finally:
        db.close()


def _activate_runtime(
    payload: object,
    *,
    root_pub: str | None = None,
    expected_entry: fleet_roster.RosterEntry | None = None,
    publish_connector: bool | None = None,
) -> fleet_runtime.FleetRuntimeCredential:
    context = _runtime_context() if root_pub is None or expected_entry is None else None
    if context is None and (root_pub is None or expected_entry is None):
        raise fleet_runtime.FleetRuntimeError(
            "this Dashboard has no active Fleet roster identity"
        )
    if context is not None:
        root_pub, expected_entry = context
    assert root_pub is not None and expected_entry is not None
    entries = tuple(fleet_roster.load_entries(org=None))
    credential = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload,
        personal_root_pub=root_pub,
        roster_entries=entries,
    )
    if credential.machine_id != expected_entry.machine_id:
        raise fleet_runtime.FleetRuntimeError(
            "fleet runtime credential names a different local machine"
        )
    _ensure_fleet_catalog(credential.machine_pub)
    fleet_sync_scheduler.configure_dashboard_fleet_sync(
        fleet_sync_scheduler.FleetSyncRuntimeConfig(
            machine_key=credential.process_key,
            roster_machine_pub=credential.machine_pub,
            delegation_cert=credential.delegation_cert,
            require_delegation=True,
            personal_root_pub=root_pub,
            roster_entries=lambda: fleet_roster.load_entries(org=None),
            # Relay-discovered peer channels replace this empty direct-address
            # map in the next transport slice. Starting the authenticated
            # runtime now is still useful: it proves unlock custody and keeps
            # the process credential lifecycle identical on first and later
            # unlocks.
            peer_addresses=lambda: {},
            personal_db_path=_org_db_path("personal"),
        )
    )
    fleet_relay_sync.dashboard_relay_sync_service.configure(credential)
    if publish_connector is None:
        tunnel = fleet_tunnel_server.state()
        publish_connector = bool(
            tunnel.allowed
            and tunnel.selected_machine_id == credential.machine_id
        )
    if publish_connector:
        fleet_relay_sync.publish_connector_runtime(payload)
    return credential


def _local_sync_phase() -> str:
    """Return the compact first-sync phase from durable local evidence.

    A successful delta pull is not enough for a newly enrolled Dashboard: it
    may contain only writes made after catalog activation.  The onboarding
    completion boundary is therefore a validated checkpoint receipt from one
    active remote roster member in the current roster epoch.  Until that
    receipt exists the truthful state remains ``synchronizing``.
    """
    local_id = machine_boot.machine_id(org="machine")
    root_pub = fleet_tunnel_server._personal_root_pub()
    if local_id is None or root_pub is None:
        return "synchronizing"
    try:
        entries = tuple(fleet_roster.load_entries(org=None))
        active = fleet_roster.resolve(entries, anchor_root_pub=root_pub)
        local_entry = next(
            (entry for entry in active.values() if entry.machine_id == local_id),
            None,
        )
        if local_entry is None or len(active) < 2:
            return "synchronizing"
        epoch = fleet_sync_scheduler.roster_epoch(entries, root_pub)
    except Exception:
        return "synchronizing"

    path = _org_db_path("personal")
    if not path.exists():
        return "synchronizing"
    remote_keys = tuple(sorted(set(active) - {local_entry.machine_pub}))
    if not remote_keys:
        return "synchronizing"
    placeholders = ",".join("?" for _ in remote_keys)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='fleet_sync_peer_state'"
        ).fetchone()
        if exists is None:
            return "synchronizing"
        row = conn.execute(
            "SELECT 1 FROM fleet_sync_peer_state "
            f"WHERE roster_epoch=? AND machine_public_key IN ({placeholders}) "
            "AND checkpoints_received>0 AND last_success_ns IS NOT NULL LIMIT 1",
            (epoch, *remote_keys),
        ).fetchone()
        return "complete" if row is not None else "synchronizing"
    except sqlite3.Error:
        return "synchronizing"
    finally:
        if "conn" in locals():
            conn.close()


async def local_sync_status(request: Request) -> JSONResponse:
    """Binary first-sync status for the post-enrollment welcome screen."""
    denied = _operator_required(request)
    if denied is not None:
        return denied
    return JSONResponse({"ok": True, "status": _local_sync_phase()})


async def resume_local_enrollment(request: Request) -> JSONResponse:
    """Advance a fresh install's existing request without a node restart.

    This route creates no request and accepts no caller-supplied identity.  It
    can only exercise the resume credential already stored in machine.db, then
    persist the parent-delivered encrypted armor and public evidence.  The
    browser still performs the password/root ceremony and machine-key proof.
    """
    from tools.init.first_run import _store_fleet_personal_armor
    from tools.network.fleet_enrollment_client import (
        FleetEnrollmentClient,
        FleetEnrollmentClientError,
    )

    state = FleetJoinStateStore()
    try:
        recovery = state.latest_any()
        if recovery is None:
            return JSONResponse({"ok": True, "status": "none"})
        if state.load_delivery(recovery.request_id) is not None:
            return JSONResponse({"ok": True, "status": "approved"})
        result = await FleetEnrollmentClient(state_store=state).resume(recovery)
        if result.status == "declined":
            state.delete(recovery.request_id)
            return JSONResponse({"ok": True, "status": "declined"})
        if result.status == "expired":
            return JSONResponse({"ok": True, "status": "expired"})
        if result.status == "approved":
            if result.delivery is None or result.personal_root_armor is None:
                raise FleetEnrollmentClientError(
                    "approved fleet enrollment returned incomplete delivery"
                )
            _store_fleet_personal_armor(
                result.personal_root_armor,
                expected_root_pub=recovery.invite.personal_root_pub,
            )
            state.save_delivery(recovery.request_id, result.delivery)
            unlock_routes.bust_enforce_cache()
            return JSONResponse({"ok": True, "status": "approved"})
        return JSONResponse({"ok": True, "status": "pending"})
    except (FleetEnrollmentClientError, ValueError, TypeError) as exc:
        return JSONResponse(
            {"ok": False, "status": "error", "error": str(exc)},
            status_code=502,
        )


async def local_completion_context(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        _state, recovery, delivery = _local_completion_state()
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if recovery is None or delivery is None:
        return JSONResponse({"ok": True, "pending": False})
    return JSONResponse({
        "ok": True,
        "pending": True,
        "request_id": recovery.request_id,
        "request": recovery.request.to_dict(),
        "invite": recovery.invite.to_dict(),
        "channel_binding": recovery.channel_binding,
        "approval": delivery.approval.to_dict(),
        "roster_entry": delivery.roster_entry.to_dict(),
        "roster_entries": [entry.to_dict() for entry in delivery.roster_entries],
    })


async def local_runtime_context(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        context = _runtime_context()
    except (ValueError, TypeError, fleet_roster.FleetRosterError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if context is None:
        return JSONResponse({"ok": True, "enabled": False})
    root_pub, entry = context
    return JSONResponse({
        "ok": True,
        "enabled": True,
        "personal_root_pub": root_pub,
        "machine_id": entry.machine_id,
        "machine_pub": entry.machine_pub,
    })


async def activate_local_runtime(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
        credential = _activate_runtime(body)
    except (
        ValueError,
        TypeError,
        fleet_runtime.FleetRuntimeError,
        fleet_roster.FleetRosterError,
    ) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "machine_id": credential.machine_id})


async def complete_local_enrollment(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    expected = {"request_id", "machine_id", "proof", "runtime"}
    if not isinstance(body, dict) or set(body) != expected:
        return JSONResponse(
            {"ok": False, "error": f"body must carry exactly {sorted(expected)}"},
            status_code=400,
        )
    try:
        state, recovery, delivery = _local_completion_state()
        if recovery is None or delivery is None:
            raise ValueError("no approved local fleet enrollment is awaiting completion")
        if body["request_id"] != recovery.request_id:
            raise ValueError("completion is for a different enrollment request")
        existing = machine_boot.machine_id(org="machine")
        if existing is None:
            machine_boot.accept_browser_completion(
                delivery,
                recovery.request,
                invite=recovery.invite,
                channel_binding=recovery.channel_binding,
                request_id=recovery.request_id,
                machine_id_value=body["machine_id"],
                proof=body["proof"],
                org="machine",
            )
        elif existing != body["machine_id"]:
            raise ValueError("this Dashboard already has a different Fleet identity")
        _activate_runtime(body["runtime"])
        state.delete(recovery.request_id)
    except (
        ValueError,
        TypeError,
        machine_boot.MachineBootError,
        fleet_runtime.FleetRuntimeError,
    ) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "machine_id": body["machine_id"]})


ROUTES = [
    Route("/api/fleet/invitations/register", register_invite, methods=["POST"]),
    Route("/api/fleet/enrollment/requests", pending_requests, methods=["GET"]),
    Route(
        "/api/fleet/enrollment/local-resume",
        resume_local_enrollment,
        methods=["POST"],
    ),
    Route(
        "/api/fleet/enrollment/local-completion",
        local_completion_context,
        methods=["GET"],
    ),
    Route(
        "/api/fleet/enrollment/local-completion",
        complete_local_enrollment,
        methods=["POST"],
    ),
    Route(
        "/api/fleet/enrollment/local-sync-status",
        local_sync_status,
        methods=["GET"],
    ),
    Route(
        "/api/fleet/runtime",
        local_runtime_context,
        methods=["GET"],
    ),
    Route(
        "/api/fleet/runtime",
        activate_local_runtime,
        methods=["POST"],
    ),
]
