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
    tunnel_scopes_down=(),
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
        tunnel_scopes_down=tunnel_scopes_down,
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
        "tunnelScopesDown": [],
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
        "tunnelScopesDown": [],
        "verdictTopLine": "LOCKED",
    }


def test_the_card_names_the_scopes_that_are_not_serving():
    """The defect this replaces: the card asked serving() with no argument,
    which means personal, so three dead org connectors on sjc-2 rendered
    "Tunnel: Serving". Naming them is what turns "something is wrong" into
    "anchore is wrong"."""
    view = project(_inputs(
        entries=(LOCAL_ENTRY,),
        tunnel_serving=False,
        tunnel_scopes_down=("anchore", "dynbench"),
    ))["localMachine"]
    assert view["tunnelServing"] is False
    assert view["tunnelScopesDown"] == ["anchore", "dynbench"]

def test_peer_counters_survive_a_roster_epoch_change(tmp_path, monkeypatch):
    """The card must not restart its counters when the roster changes.

    fleet_sync_peer_state is keyed (machine_public_key, roster_epoch), so a
    join or a kick starts a fresh row at zero. While this view read only the
    current epoch, "Changes applied" reset while the byte totals beside it --
    epoch-free telemetry -- kept climbing. On 2026-09-09 that rendered as
    49 GB sent to a peer and 0 changes applied to it, whose honest reading is
    that synchronization is broken. It was not.
    """
    import sqlite3

    from tools.dashboard.plugins.fleet.entrypoints import projection as proj

    path = tmp_path / "personal.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE fleet_sync_peer_state("
        " machine_public_key TEXT, roster_epoch TEXT, last_success_ns INTEGER,"
        " bytes_sent INTEGER, bytes_received INTEGER, transactions_applied INTEGER,"
        " acknowledgements INTEGER, retries INTEGER, last_error_code TEXT,"
        " peer_watermark INTEGER, local_watermark INTEGER, online INTEGER,"
        " lag_ns INTEGER, updated_at_ns INTEGER,"
        " PRIMARY KEY(machine_public_key, roster_epoch))"
    )
    peer = "cd" * 32
    # An older epoch carrying the bulk of the history, and the current one
    # holding only what has happened since the roster changed.
    conn.execute(
        "INSERT INTO fleet_sync_peer_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (peer, "old" + "0" * 61, 1_000, 900, 90, 500, 0, 7, "stale_error",
         5_000, 0, 1, 0, 1_000),
    )
    conn.execute(
        "INSERT INTO fleet_sync_peer_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (peer, "new" + "0" * 61, 2_000, 100, 10, 3, 0, 1, None,
         9_000, 0, 1, 0, 2_000),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(proj, "_org_db_path", lambda name: path)

    row = proj._peer_rows("new" + "0" * 61)[peer]

    assert row["transactions_applied"] == 503, "counters restarted at the epoch"
    assert row["retries"] == 8
    assert row["bytes_sent"] == 1000
    # Point-in-time facts take their newest value, not their sum.
    assert row["last_success_ns"] == 2_000
    assert row["peer_watermark"] == 9_000
    # An error from a retired epoch is not this peer's current state.
    assert row["last_error_code"] is None


def test_a_quiet_organization_is_not_reported_as_lag():
    """The card said anchore 47h and blindhash 13h while both machines held
    byte-identical positions on every scope and the reverse direction had just
    drained to zero. `now - their_frontier` measures how OLD the content is;
    on an organization nobody has written to in two days that is two days of
    invented lag on a perfectly converged peer."""
    from tools.dashboard.plugins.fleet.entrypoints import projection as proj

    two_days_ago = (NOW - 2 * 24 * 3_600_000) * 1_000_000
    [row] = proj._scope_rows(
        [{"scope": "anchore", "frontier_ns": two_days_ago}],
        server_time=NOW,
        local={"anchore": two_days_ago},
    )
    assert row["lag"] == 0, "a converged peer must not read as behind"


def test_a_peer_that_trails_us_reports_the_difference():
    from tools.dashboard.plugins.fleet.entrypoints import projection as proj

    theirs = (NOW - 90_000) * 1_000_000
    [row] = proj._scope_rows(
        [{"scope": "autonomy", "frontier_ns": theirs}],
        server_time=NOW,
        local={"autonomy": NOW * 1_000_000},
    )
    assert row["lag"] == 90_000


def test_no_local_position_reports_unknown_rather_than_converged():
    """We hold nothing for that scope, so we have no reference. Null renders as
    unknown; zero would claim agreement we cannot see."""
    from tools.dashboard.plugins.fleet.entrypoints import projection as proj

    [row] = proj._scope_rows(
        [{"scope": "blindhash", "frontier_ns": 5}], server_time=NOW, local={},
    )
    assert row["lag"] is None


def test_the_local_machine_gets_its_own_organization_rows():
    """It has no peer row about ITSELF -- Record 2 is keyed by peer -- so the
    local card rendered an empty table and "0 / 0", which reads as a fault
    rather than as the category error it is: this machine has nothing to be
    behind. Bytes are its real totals per organization, summed across the
    peers it exchanged them with."""
    from tools.dashboard.plugins.fleet.entrypoints import projection as proj

    peers = {
        "aa": [{"scope": "personal", "bytes_in": 100, "bytes_out": 10},
               {"scope": "anchore", "bytes_in": 5, "bytes_out": 1}],
        "bb": [{"scope": "personal", "bytes_in": 50, "bytes_out": 5}],
    }
    rows = {r["scope"]: r for r in proj._local_scope_rows(
        peers, {"personal": 1, "anchore": 1}, {})}

    assert rows["personal"]["bytesIn"] == 150
    assert rows["personal"]["bytesOut"] == 15
    assert rows["anchore"]["bytesIn"] == 5
    assert all(r["lag"] == 0 for r in rows.values()), "the reference is not behind"
    assert all(r["filling"] is None for r in rows.values())


def test_a_scope_mid_bootstrap_reports_how_much_is_left():
    """A machine filling a scope knows exactly how far it has to go: F is the
    serving store's frontier captured at sweep start and persisted because it
    is not derivable from the receiving database. Until the bootstrap
    completes the store advertises nothing, so without this it looks idle."""
    from tools.dashboard.plugins.fleet.entrypoints import projection as proj

    ours = 1_000_000_000
    target = ours + 60_000 * 1_000_000          # a minute of content to go
    [row] = [r for r in proj._local_scope_rows(
        {}, {"personal": ours},
        {"personal": {"phase": "sweeping", "frontier": {"origin": target}}},
    ) if r["scope"] == "personal"]

    assert row["filling"] == "sweeping"
    assert row["lag"] == 60_000, "must say how much remains, not zero"
