from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tools.dashboard.fleet_enrollment_service import StoredFleetInvitation
from tools.dashboard.plugins.fleet.entrypoints.projection import (
    ProjectionInputs,
    project,
)
from tools.network import fleet_invite, fleet_roster
from tools.network.idkit import KeyPair


NOW = 1_777_000_000_000


def _roster_entry(root: KeyPair, machine: KeyPair, machine_id: str, issued: int):
    return fleet_roster.enroll(
        root,
        machine_id=machine_id,
        machine_pub=machine.public_hex,
        issued_at=issued,
    )


def _admission(approval_id: str, *, status="pending", error=None, offset=0):
    return SimpleNamespace(
        source_approval_id=approval_id,
        status=status,
        last_error_code=error,
        created_at=NOW - 10_000 + offset,
        updated_at=NOW - 5_000 + offset,
    )


def _inputs(
    *, entries=(), admissions=(), approvals=None, executing=(),
    invitation=None, deactivated_invitation=None,
    machine_names=None, invitation_publication=None,
    publishing_org="autonomy", telemetry_rows=None,
    local_verdict=None, serve_cert=None, tunnel_serving=None,
):
    root = ROOT
    return ProjectionInputs(
        server_time=NOW,
        root_pub=root.public_hex,
        roster_entries=tuple(entries),
        local_machine_id=LOCAL_ID,
        selected_machine_id=LOCAL_ID,
        peer_rows={
            REMOTE.public_hex: {
                "last_success_ns": (NOW - 2_000) * 1_000_000,
                "transactions_applied": 12,
                "bytes_sent": 100,
                "bytes_received": 300,
                "retries": 1,
                "last_error_code": None,
            },
        },
        admissions=tuple(admissions),
        approvals=approvals or {},
        executing_approval_ids=frozenset(executing),
        invitation=invitation,
        deactivated_invitation=deactivated_invitation,
        machine_names=machine_names or {},
        invitation_publication=invitation_publication,
        publishing_org=publishing_org,
        telemetry_rows=telemetry_rows or {},
        local_verdict=local_verdict,
        serve_cert=serve_cert,
        tunnel_serving=tunnel_serving,
    )


ROOT = KeyPair.generate()
LOCAL = KeyPair.generate()
REMOTE = KeyPair.generate()
LOCAL_ID = "11" * 32
REMOTE_ID = "22" * 32
LOCAL_ENTRY = _roster_entry(ROOT, LOCAL, LOCAL_ID, NOW - 60_000)
REMOTE_ENTRY = _roster_entry(ROOT, REMOTE, REMOTE_ID, NOW - 30_000)


def test_projection_joins_roster_local_tunnel_and_current_epoch_observations():
    view = project(_inputs(entries=(LOCAL_ENTRY, REMOTE_ENTRY)))

    assert view["summary"] == {
        "authorizedMachines": 2,
        "connectedMachines": None,
        "lastSuccessfulSyncAt": NOW - 2_000,
        "joinRequests": 0,
    }
    local, remote = view["machines"]
    assert local["machineId"] == LOCAL_ID
    assert local["isLocalMachine"] is True
    assert local["isTunnelServer"] is True
    assert local["presence"] == "unreported"
    assert remote["machineId"] == REMOTE_ID
    assert remote["displayLabel"] == "Untitled machine"
    assert remote["transactionsApplied"] == 12
    assert remote["bytesSent"] + remote["bytesReceived"] == 400
    assert view["activity"]["transactionsApplied"] == 12
    assert view["activity"]["scope"] == "this_dashboard_current_roster"


def test_machine_local_telemetry_drives_transfer_and_iteration_counters():
    view = project(_inputs(
        entries=(LOCAL_ENTRY, REMOTE_ENTRY),
        telemetry_rows={
            REMOTE.public_hex: {
                "iterations": 4,
                "successful_iterations": 3,
                "failed_iterations": 1,
                "cancelled_iterations": 0,
                "total_duration_ms": 412_500,
                "last_duration_ms": 2_500,
                "bytes_sent": 61_460_671,
                "bytes_received": 512,
                "mutation_frames": 347_760,
                "transactions": 64_579,
                "last_outcome": "success",
                "last_success_at_ns": (NOW - 500) * 1_000_000,
            },
        },
    ))

    remote = next(row for row in view["machines"] if row["machineId"] == REMOTE_ID)
    assert remote["syncIterations"] == 4
    assert remote["successfulIterations"] == 3
    assert remote["failedIterations"] == 1
    assert remote["lastSyncDurationMs"] == 2_500
    assert remote["totalSyncDurationMs"] == 412_500
    assert remote["bytesSent"] == 61_460_671
    assert remote["bytesReceived"] == 512
    assert remote["mutationFrames"] == 347_760
    assert remote["transactionsTransferred"] == 64_579
    assert remote["lastSuccessfulSyncAt"] == NOW - 500
    assert view["activity"]["syncIterations"] == 4
    assert view["activity"]["totalSyncDurationMs"] == 412_500


def test_human_machine_name_overrides_unsigned_fallback_label():
    view = project(_inputs(
        entries=(LOCAL_ENTRY, REMOTE_ENTRY),
        machine_names={REMOTE_ID: "SJC dashboard"},
    ))
    remote = next(row for row in view["machines"] if row["machineId"] == REMOTE_ID)
    assert remote["displayLabel"] == "SJC dashboard"


def test_multiple_admissions_are_rows_not_a_fleet_approval_queue():
    pending = _admission("fleet-pending")
    executing = _admission("fleet-executing", offset=1)
    failed = _admission(
        "fleet-failed", status="failed", error="approval_execution_failed", offset=2
    )
    declined = _admission("fleet-declined", offset=3)
    view = project(_inputs(
        entries=(LOCAL_ENTRY,),
        admissions=(pending, executing, failed, declined),
        approvals={
            "fleet-pending": {"result": None},
            "fleet-executing": {"result": None},
            "fleet-failed": {"result": {"approved": True, "execution": {"ok": False}}},
            "fleet-declined": {"result": {"approved": False}},
        },
        executing=("fleet-executing",),
    ))

    candidates = [r for r in view["machines"] if r["rowKind"] == "pending_admission"]
    assert [row["standing"] for row in candidates] == [
        "pending_approval", "admission_in_progress", "admission_failed",
    ]
    assert view["summary"]["authorizedMachines"] == 1
    assert view["summary"]["joinRequests"] == 3
    assert all(row["machineId"] is None for row in candidates)
    assert all(row["canRemove"] is False for row in candidates)


def test_missing_correlated_approval_fails_visible_instead_of_claiming_review():
    view = project(_inputs(
        entries=(LOCAL_ENTRY,),
        admissions=(_admission("fleet-missing"),),
        approvals={"fleet-missing": None},
    ))
    candidate = view["machines"][1]
    assert candidate["standing"] == "admission_failed"
    assert candidate["lastErrorCode"] == "approval_record_missing"


def test_roster_commit_replaces_candidate_with_only_signed_roster_truth():
    before = project(_inputs(
        entries=(LOCAL_ENTRY,),
        admissions=(_admission("fleet-remote"),),
        approvals={"fleet-remote": {"result": None}},
    ))
    after = project(_inputs(entries=(LOCAL_ENTRY, REMOTE_ENTRY)))

    assert before["summary"]["authorizedMachines"] == 1
    assert before["machines"][1]["sourceApprovalId"] == "fleet-remote"
    assert before["machines"][1]["machinePublicKey"] is None
    assert after["summary"]["authorizedMachines"] == 2
    assert not any(row["rowKind"] == "pending_admission" for row in after["machines"])
    assert any(row["entryId"] == REMOTE_ENTRY.entry_id for row in after["machines"])


def test_revocation_is_root_signed_history_and_never_removable():
    kicked = fleet_roster.kick(
        ROOT, machine_id=REMOTE_ID, machine_pub=REMOTE.public_hex,
        seq=1, issued_at=NOW,
    )
    view = project(_inputs(entries=(LOCAL_ENTRY, REMOTE_ENTRY, kicked)))
    revoked = next(row for row in view["machines"] if row["standing"] == "revoked")
    assert revoked["machineId"] == REMOTE_ID
    assert revoked["presence"] == "blocked"
    assert revoked["canRemove"] is False
    assert view["summary"]["authorizedMachines"] == 1


def test_active_invitation_projects_short_link_and_signed_bootstrap_value():
    invite = fleet_invite.mint(
        ROOT,
        rendezvous="https://primary.example.test/links/token-1",
        invite_id="33" * 32,
        expires_at=NOW + 86_400_000,
    )
    stored = StoredFleetInvitation(
        target_uuid="11111111-1111-4111-8111-111111111111",
        invite=invite,
        active=True,
        created_at=NOW - 10_000,
    )
    value = project(_inputs(entries=(LOCAL_ENTRY,), invitation=stored))["invitation"]
    assert value["status"] == "active"
    assert value["url"] == invite.rendezvous
    assert value["bootstrapCode"] == "AUTONOMY_FLEET_INVITE=" + fleet_invite.encode(invite)
    assert value["publishedAt"] == NOW - 10_000
    assert value["expiresAt"] == NOW + 86_400_000
    # The stable target id rides along so the browser can deactivate the
    # invitation and post the matching link_revoke approval.
    assert value["targetUuid"] == "11111111-1111-4111-8111-111111111111"


def test_deactivated_invitation_projects_inactive_for_reactivation():
    # A signed-but-deactivated invite must read 'inactive' (offer Reactivate),
    # NOT fall through to 'awaiting_signature' — which would re-mint and strand
    # a machine pinned to this invitation. The still-present publication record
    # must not win over the dormant signed invite.
    invite = fleet_invite.mint(
        ROOT,
        rendezvous="https://primary.example.test/links/token-2",
        invite_id="44" * 32,
        expires_at=NOW + 86_400_000,
    )
    dormant = StoredFleetInvitation(
        target_uuid="22222222-2222-4222-8222-222222222222",
        invite=invite,
        active=False,
        created_at=NOW - 10_000,
    )
    publication = {
        "id": "publish-two",
        "created_at": (NOW - 10_000) / 1000,
        "request": {
            "org": "autonomy",
            "target_uuid": "22222222-2222-4222-8222-222222222222",
            "target_type": "fleet:join",
            "meta": {"ttl": 604800},
        },
        "result": {
            "approved": True,
            "execution": {"ok": True, "url": invite.rendezvous, "token": "44" * 16},
        },
    }
    value = project(_inputs(
        entries=(LOCAL_ENTRY,),
        deactivated_invitation=dormant,
        invitation_publication=publication,
    ))["invitation"]
    assert value["status"] == "inactive"
    assert value["url"] == invite.rendezvous
    assert value["bootstrapCode"] == "AUTONOMY_FLEET_INVITE=" + fleet_invite.encode(invite)
    assert value["targetUuid"] == "22222222-2222-4222-8222-222222222222"


def test_published_route_waits_for_browser_personal_signature():
    publication = {
        "id": "publish-one",
        "created_at": (NOW - 10_000) / 1000,
        "request": {
            "org": "autonomy",
            "target_uuid": "11111111-1111-4111-8111-111111111111",
            "target_type": "fleet:join",
            "meta": {"ttl": 604800},
        },
        "result": {
            "approved": True,
            "execution": {
                "ok": True,
                "url": "https://relay.auto.network/l/" + "45" * 16,
                "token": "45" * 16,
            },
        },
    }
    value = project(_inputs(
        entries=(LOCAL_ENTRY,), invitation_publication=publication,
    ))["invitation"]

    assert value == {
        "status": "awaiting_signature",
        "url": None,
        "publishedAt": NOW - 10_000,
        "expiresAt": NOW - 10_000 + 604_800_000,
        "publishingOrg": "autonomy",
        "targetUuid": "11111111-1111-4111-8111-111111111111",
        "error": None,
        "rendezvous": "https://relay.auto.network/l/" + "45" * 16,
        "grantToken": "45" * 16,
    }


def test_pending_publication_is_not_presented_as_an_active_invite():
    value = project(_inputs(
        entries=(LOCAL_ENTRY,),
        invitation_publication={
            "id": "publish-one",
            "created_at": NOW / 1000,
            "request": {
                "org": "autonomy",
                "target_uuid": "11111111-1111-4111-8111-111111111111",
                "target_type": "fleet:join",
                "meta": {"ttl": 604800},
            },
            "result": None,
        },
    ))["invitation"]
    assert value["status"] == "publishing"
    assert value["url"] is None


def test_projection_exposes_no_fleet_decision_surface():
    view = project(_inputs(
        entries=(LOCAL_ENTRY,),
        admissions=(_admission("fleet-pending"),),
        approvals={"fleet-pending": {"result": None}},
    ))
    wire = json.dumps(view)
    assert "verification_code" not in wire
    assert "channel_binding" not in wire
    assert "approval_json" not in wire



def test_local_machine_block_reports_probe_facts_or_stays_null():
    """An unreadable probe yields nulls (the browser renders nothing), never a
    guessed healthy/unhealthy claim."""
    absent = project(_inputs(entries=(LOCAL_ENTRY,)))["localMachine"]
    assert absent == {
        "connectorArmed": None,
        "runningStale": None,
        "certStatus": None,
        "certValidUntil": None,
        "tunnelServing": None,
        "verdictTopLine": None,
    }

    view = project(_inputs(
        entries=(LOCAL_ENTRY,),
        local_verdict={
            "top_line": "LOCKED",
            "credential": {"configured": False},
            "connector_version": {"status": "ok"},
            "dashboard_version": {"status": "stale"},
        },
        serve_cert={"status": "ok", "not_after": 1_777_086_400},
        tunnel_serving=False,
    ))["localMachine"]
    assert view == {
        "connectorArmed": False,
        "runningStale": True,
        "certStatus": "ok",
        "certValidUntil": 1_777_086_400_000,
        "tunnelServing": False,
        "verdictTopLine": "LOCKED",
    }
