"""Truthful read model for the Fleet Machines surface.

The browser consumes one projection rather than independently joining the
personal roster, machine identity, tunnel assignment, sync observations,
invitation transport, and generic approval rendezvous.  Approval ids are an
internal correlation seam only; this module never exposes decision controls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3
import time
from typing import Mapping

from tools.dashboard.dao import approval_requests
from tools.dashboard.fleet_enrollment_service import (
    FleetEnrollmentStore,
    PendingEnrollment,
    StoredFleetInvitation,
)
from tools.graph.db import _org_db_path
from tools.network import (
    fleet_invite,
    fleet_machine_profile,
    fleet_roster,
    fleet_sync_scheduler,
    fleet_sync_telemetry,
    fleet_tunnel_server,
    machine_boot,
)


@dataclass(frozen=True)
class ProjectionInputs:
    server_time: int
    root_pub: str | None
    roster_entries: tuple[fleet_roster.RosterEntry, ...]
    local_machine_id: str | None
    selected_machine_id: str | None
    peer_rows: Mapping[str, Mapping]
    admissions: tuple[PendingEnrollment, ...]
    approvals: Mapping[str, Mapping | None]
    executing_approval_ids: frozenset[str]
    invitation: StoredFleetInvitation | None
    deactivated_invitation: StoredFleetInvitation | None = None
    machine_names: Mapping[str, str] = field(default_factory=dict)
    invitation_publication: Mapping | None = None
    publishing_org: str = "personal"
    telemetry_rows: Mapping[str, Mapping] = field(default_factory=dict)
    local_verdict: Mapping | None = None
    serve_cert: Mapping | None = None
    tunnel_serving: bool | None = None


def _peer_rows(epoch: str | None) -> dict[str, dict]:
    """Current-epoch observations from this Dashboard's personal database."""
    if epoch is None:
        return {}
    path = _org_db_path("personal")
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='fleet_sync_peer_state'"
        ).fetchone()
        if exists is None:
            return {}
        cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(fleet_sync_peer_state)")
        }
        built = ",peer_built_at" if "peer_built_at" in cols else ""
        rows = conn.execute(
            "SELECT machine_public_key,last_success_ns,bytes_sent,"
            "bytes_received,transactions_applied,retries,last_error_code,"
            f"updated_at_ns{built} FROM fleet_sync_peer_state WHERE roster_epoch=?",
            (epoch,),
        ).fetchall()
        return {str(row["machine_public_key"]): dict(row) for row in rows}
    finally:
        if "conn" in locals():
            conn.close()


def _load_inputs(*, now_ms: int) -> ProjectionInputs:
    root_pub = fleet_tunnel_server._personal_root_pub()
    entries = tuple(fleet_roster.load_entries(org=None))
    if entries and root_pub is None:
        raise ValueError("Fleet roster exists but its personal root is unavailable")
    epoch = None
    if root_pub is not None:
        try:
            epoch = fleet_sync_scheduler.roster_epoch(entries, root_pub)
        except Exception:
            epoch = None
    local_machine_id = machine_boot.machine_id(org="machine")
    selected_machine_id = fleet_tunnel_server.state().selected_machine_id
    store = FleetEnrollmentStore()
    admissions = store.list_admissions()
    approvals = {
        row.source_approval_id: approval_requests.get(row.source_approval_id)
        for row in admissions
        if row.source_approval_id
    }
    from tools.dashboard import approvals_routes

    executing = frozenset(
        approval_id for approval_id in approvals
        if approvals_routes.approval_is_executing(approval_id)
    )
    invitation_publication = next(
        (
            row
            for row in approval_requests.recent_for_kind("link_publish")
            if (row.get("request") or {}).get("target_type") == "fleet:join"
        ),
        None,
    )
    # The local machine's operational facts (connector armed, code staleness,
    # serving certificate). Probes are best-effort: an unreadable probe leaves
    # the field null and the browser shows nothing rather than a guess.
    local_verdict = None
    try:
        from tools.network.fleet_verdict import compute_verdict

        local_verdict = compute_verdict(None)
    except Exception:
        local_verdict = None
    serve_cert = None
    try:
        from tools.dashboard.link_serving_supervisor import serve_cert_state

        serve_cert = serve_cert_state(None)
    except Exception:
        serve_cert = None
    # The same live handshake probe the profile panel's Tunnel indicator uses
    # (unlock-state): True only when the connector child reports a completed
    # tunnel handshake. None when the probe is unavailable.
    tunnel_serving = None
    try:
        from tools.dashboard.link_serving_supervisor import get_supervisor

        tunnel_serving = bool(get_supervisor().serving())
    except Exception:
        tunnel_serving = None
    return ProjectionInputs(
        server_time=now_ms,
        root_pub=root_pub,
        roster_entries=entries,
        local_machine_id=local_machine_id,
        selected_machine_id=selected_machine_id,
        peer_rows=_peer_rows(epoch),
        admissions=admissions,
        approvals=approvals,
        executing_approval_ids=executing,
        invitation=store.current_invitation(now_ms=now_ms),
        deactivated_invitation=store.deactivated_invitation(now_ms=now_ms),
        machine_names=fleet_machine_profile.names(org=None),
        invitation_publication=invitation_publication,
        publishing_org="personal",
        telemetry_rows=fleet_sync_telemetry.read_peer_totals(org="machine"),
        local_verdict=local_verdict,
        serve_cert=serve_cert,
        tunnel_serving=tunnel_serving,
    )


def _display_id(machine_id: str) -> str:
    return f"{machine_id[:6]}…{machine_id[-6:]}"


def _milliseconds(value) -> int | None:
    if value is None:
        return None
    number = int(value)
    return number // 1_000_000 if number > 1_000_000_000_000_000 else number


def _observation(peer: Mapping | None, telemetry: Mapping | None = None) -> dict:
    peer = peer or {}
    telemetry = telemetry or {}
    has_telemetry = bool(telemetry.get("iterations"))
    return {
        "lastSuccessfulSyncAt": max(
            filter(
                lambda value: value is not None,
                (
                    _milliseconds(peer.get("last_success_ns")),
                    _milliseconds(telemetry.get("last_success_at_ns")),
                ),
            ),
            default=None,
        ),
        "transactionsApplied": int(peer.get("transactions_applied") or 0),
        "bytesSent": int(
            telemetry.get("bytes_sent") if has_telemetry
            else peer.get("bytes_sent") or 0
        ),
        "bytesReceived": int(
            telemetry.get("bytes_received") if has_telemetry
            else peer.get("bytes_received") or 0
        ),
        "retryCount": int(peer.get("retries") or 0),
        "lastErrorCode": peer.get("last_error_code"),
        # The peer's build (committer date), learned from a schema refusal, so a
        # version-mismatch ("Paused") row can name WHICH build the peer runs.
        "peerBuiltAt": peer.get("peer_built_at"),
        "syncIterations": int(telemetry.get("iterations") or 0),
        "successfulIterations": int(
            telemetry.get("successful_iterations") or 0
        ),
        "failedIterations": int(telemetry.get("failed_iterations") or 0),
        "totalSyncDurationMs": int(telemetry.get("total_duration_ms") or 0),
        "lastSyncDurationMs": int(telemetry.get("last_duration_ms") or 0),
        "mutationFrames": int(telemetry.get("mutation_frames") or 0),
        "transactionsTransferred": int(telemetry.get("transactions") or 0),
        "lastSyncOutcome": telemetry.get("last_outcome"),
    }


def _machine_row(
    entry: fleet_roster.RosterEntry,
    *,
    standing: str,
    local_machine_id: str | None,
    selected_machine_id: str | None,
    peer: Mapping | None,
    telemetry: Mapping | None,
    display_name: str | None,
) -> dict:
    local = entry.machine_id == local_machine_id
    return {
        "rowKind": "roster_machine",
        "sourceApprovalId": None,
        "entryId": entry.entry_id,
        "machineId": entry.machine_id,
        "machinePublicKey": entry.machine_pub,
        # The current per-machine sequence — the kick ceremony mints seq+1 so
        # its tombstone beats the entry it revokes (fleet-kick.js / the /kick
        # route both rely on this being the resolved winner's seq).
        "seq": entry.seq,
        "displayLabel": display_name or (
            "This dashboard" if local else "Untitled machine"
        ),
        "isLocalMachine": local,
        "isTunnelServer": entry.machine_id == selected_machine_id,
        "standing": standing,
        "assignment": entry.assignment,
        "standingChangedAt": entry.issued_at,
        "presence": "blocked" if standing == "revoked" else "unreported",
        **_observation(
            peer if standing == "authorized" else None,
            telemetry if standing == "authorized" else None,
        ),
        # The browser-root removal command is deliberately not invented by
        # this read-only slice.
        "canRemove": False,
    }


def _revoked_entries(
    entries: tuple[fleet_roster.RosterEntry, ...],
    *,
    root_pub: str,
    active_public_keys: set[str],
) -> list[fleet_roster.RosterEntry]:
    verified: list[fleet_roster.RosterEntry] = []
    for entry in entries:
        try:
            fleet_roster.verify(entry, anchor_root_pub=root_pub)
        except fleet_roster.FleetRosterError:
            continue
        verified.append(entry)
    by_machine: dict[str, list[fleet_roster.RosterEntry]] = {}
    for entry in verified:
        by_machine.setdefault(entry.machine_pub, []).append(entry)
    revoked = []
    for machine_pub, machine_entries in by_machine.items():
        if machine_pub in active_public_keys:
            continue
        cited = {
            entry.supersedes for entry in machine_entries
            if entry.kind == fleet_roster.EntryKind.ENROLL
            and entry.supersedes is not None
        }
        live_kicks = [
            entry for entry in machine_entries
            if entry.kind == fleet_roster.EntryKind.KICK
            and entry.entry_id not in cited
        ]
        if live_kicks:
            live_kicks.sort(key=lambda entry: (-entry.seq, entry.entry_id))
            revoked.append(live_kicks[0])
    return revoked


def _admission_standing(
    row: PendingEnrollment,
    *,
    approval: Mapping | None,
    executing: bool,
) -> tuple[str, str | None] | None:
    if row.status == "failed":
        return "admission_failed", row.last_error_code or "admission_failed"
    if not row.source_approval_id:
        return "admission_failed", "approval_registration_missing"
    if row.status == "approving" or executing:
        return "admission_in_progress", None
    if approval is None:
        return "admission_failed", "approval_record_missing"
    result = (approval or {}).get("result")
    if result is None:
        return "pending_approval", None
    if result.get("approved") is not True:
        return None
    execution = result.get("execution")
    if isinstance(execution, Mapping) and execution.get("ok") is False:
        return "admission_failed", "approval_execution_failed"
    # A completed grant may only disappear in favour of committed roster
    # truth. If that invariant breaks, keep a safe, visible failure row.
    return "admission_failed", "roster_commit_missing"


def _admission_row(
    row: PendingEnrollment, standing: str, error_code: str | None
) -> dict:
    return {
        "rowKind": "pending_admission",
        "sourceApprovalId": row.source_approval_id,
        "entryId": None,
        "machineId": None,
        "machinePublicKey": None,
        "displayLabel": "New machine",
        "isLocalMachine": False,
        "isTunnelServer": False,
        "standing": standing,
        "assignment": None,
        "standingChangedAt": row.updated_at,
        "presence": "not_applicable",
        "lastSuccessfulSyncAt": None,
        "transactionsApplied": 0,
        "bytesSent": 0,
        "bytesReceived": 0,
        "retryCount": 0,
        "lastErrorCode": error_code,
        "syncIterations": 0,
        "successfulIterations": 0,
        "failedIterations": 0,
        "totalSyncDurationMs": 0,
        "lastSyncDurationMs": 0,
        "mutationFrames": 0,
        "transactionsTransferred": 0,
        "lastSyncOutcome": None,
        "canRemove": False,
    }


def project(inputs: ProjectionInputs) -> dict:
    active: dict[str, fleet_roster.RosterEntry] = {}
    if inputs.root_pub is not None:
        active = fleet_roster.resolve(
            inputs.roster_entries, anchor_root_pub=inputs.root_pub
        )
    roster_rows = [
        _machine_row(
            entry,
            standing="authorized",
            local_machine_id=inputs.local_machine_id,
            selected_machine_id=inputs.selected_machine_id,
            peer=inputs.peer_rows.get(machine_pub),
            telemetry=inputs.telemetry_rows.get(machine_pub),
            display_name=inputs.machine_names.get(entry.machine_id),
        )
        for machine_pub, entry in active.items()
    ]
    roster_rows.sort(
        key=lambda row: (not row["isLocalMachine"], row["machineId"] or "")
    )
    revoked_rows = []
    if inputs.root_pub is not None:
        revoked_rows = [
            _machine_row(
                entry,
                standing="revoked",
                local_machine_id=inputs.local_machine_id,
                selected_machine_id=inputs.selected_machine_id,
                peer=None,
                telemetry=None,
                display_name=inputs.machine_names.get(entry.machine_id),
            )
            for entry in _revoked_entries(
                inputs.roster_entries,
                root_pub=inputs.root_pub,
                active_public_keys=set(active),
            )
        ]
        revoked_rows.sort(key=lambda row: row["machineId"] or "")

    admission_rows = []
    for row in inputs.admissions:
        approval_id = row.source_approval_id
        lifecycle = _admission_standing(
            row,
            approval=inputs.approvals.get(approval_id) if approval_id else None,
            executing=bool(approval_id and approval_id in inputs.executing_approval_ids),
        )
        if lifecycle is None:
            continue
        admission_rows.append(_admission_row(row, *lifecycle))

    observations = [inputs.peer_rows.get(machine_pub) or {} for machine_pub in active]
    last_successes = [
        _milliseconds(row.get("last_success_ns")) for row in observations
        if row.get("last_success_ns") is not None
    ]
    invitation = inputs.invitation
    invitation_view = {
        "status": "none",
        "url": None,
        "publishedAt": None,
        "expiresAt": None,
        "publishingOrg": inputs.publishing_org,
        "error": None,
    }
    if invitation is not None:
        invitation_view = {
            "status": "active",
            "targetUuid": invitation.target_uuid,
            "url": invitation.invite.rendezvous,
            "bootstrapCode": "AUTONOMY_FLEET_INVITE=" + fleet_invite.encode(invitation.invite),
            "publishedAt": invitation.created_at,
            "expiresAt": invitation.invite.expires_at or None,
            "publishingOrg": inputs.publishing_org,
            "error": None,
        }
    elif inputs.deactivated_invitation is not None:
        # Signed but deactivated. The route stands and the invite is intact;
        # only the active flag is off. Offer Reactivate (flip THIS invite back
        # on) rather than the fall-through 'awaiting_signature', which would
        # re-mint and strand any machine pinned to this invitation.
        deactivated = inputs.deactivated_invitation
        invitation_view = {
            "status": "inactive",
            "targetUuid": deactivated.target_uuid,
            "url": deactivated.invite.rendezvous,
            "bootstrapCode": "AUTONOMY_FLEET_INVITE=" + fleet_invite.encode(deactivated.invite),
            "publishedAt": deactivated.created_at,
            "expiresAt": deactivated.invite.expires_at or None,
            "publishingOrg": inputs.publishing_org,
            "error": None,
        }
    elif inputs.invitation_publication is not None:
        publication = inputs.invitation_publication
        request = publication.get("request") or {}
        result = publication.get("result")
        created_at = int(float(publication.get("created_at") or 0) * 1000)
        ttl = (request.get("meta") or {}).get("ttl")
        if isinstance(result, Mapping) and "ttl" in result:
            ttl = result.get("ttl")
        expires_at = created_at + ttl * 1000 if type(ttl) is int else None
        common = {
            "url": None,
            "publishedAt": created_at or None,
            "expiresAt": expires_at,
            "publishingOrg": request.get("org") or inputs.publishing_org,
            "targetUuid": request.get("target_uuid"),
            "error": None,
        }
        if result is None:
            invitation_view = {**common, "status": "publishing"}
        elif result.get("approved") is not True:
            invitation_view = {
                **common,
                "status": "failed",
                "error": "Invitation publication was declined.",
            }
        else:
            execution = result.get("execution")
            if isinstance(execution, Mapping) and execution.get("ok") is True:
                invitation_view = {
                    **common,
                    "status": "awaiting_signature",
                    "rendezvous": execution.get("url"),
                    "grantToken": execution.get("token"),
                }
            else:
                invitation_view = {
                    **common,
                    "status": "failed",
                    "error": (
                        execution.get("error")
                        if isinstance(execution, Mapping)
                        else "Invitation publication did not complete."
                    ),
                }
    verdict = inputs.local_verdict or {}
    credential = verdict.get("credential") or {}
    cert = inputs.serve_cert or {}
    cert_not_after = cert.get("not_after")
    local_machine = {
        # False only when the probe positively reported an unconfigured
        # credential; None (probe unavailable) renders nothing.
        "connectorArmed": (
            None if credential.get("configured") is None
            else bool(credential.get("configured"))
        ),
        "runningStale": (
            (verdict.get("connector_version") or {}).get("status") == "stale"
            or (verdict.get("dashboard_version") or {}).get("status") == "stale"
        ) if verdict else None,
        "certStatus": cert.get("status"),
        "certValidUntil": (
            cert_not_after * 1000 if isinstance(cert_not_after, int) else None
        ),
        "tunnelServing": inputs.tunnel_serving,
        "verdictTopLine": verdict.get("top_line"),
    }
    from tools.network import build_version
    return {
        "serverTime": inputs.server_time,
        "localMachine": local_machine,
        # This machine's build (committer date), so a version-mismatch row can
        # read "this build … / peer's build …" instead of an opaque digest.
        "localBuiltAt": build_version.disk_built_at(),
        "summary": {
            "authorizedMachines": len(active),
            "connectedMachines": None,
            "lastSuccessfulSyncAt": max(last_successes) if last_successes else None,
            "joinRequests": len(admission_rows),
        },
        "machines": [*roster_rows, *admission_rows, *revoked_rows],
        "invitation": invitation_view,
        "activity": {
            "transactionsApplied": sum(row["transactionsApplied"] for row in roster_rows),
            "bytesSent": sum(row["bytesSent"] for row in roster_rows),
            "bytesReceived": sum(row["bytesReceived"] for row in roster_rows),
            "syncIterations": sum(row["syncIterations"] for row in roster_rows),
            "successfulIterations": sum(
                row["successfulIterations"] for row in roster_rows
            ),
            "failedIterations": sum(row["failedIterations"] for row in roster_rows),
            "totalSyncDurationMs": sum(
                row["totalSyncDurationMs"] for row in roster_rows
            ),
            "mutationFrames": sum(row["mutationFrames"] for row in roster_rows),
            "scope": "this_dashboard_current_roster",
        },
    }


def build_view(*, now_ms: int | None = None) -> dict:
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return project(_load_inputs(now_ms=now))
