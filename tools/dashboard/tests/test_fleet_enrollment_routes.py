"""Operator API acceptance for the fleet invitation rendezvous."""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import (
    approvals_routes,
    fleet_enrollment_approvals,
    fleet_enrollment_routes,
    fleet_enrollment_service,
    identity_routes,
    link_serving,
    unlock_routes,
)
from tools.dashboard.dao import approval_requests as ar
from tools.graph.db import GraphDB
from tools.network import (
    fleet_enroll,
    fleet_invite,
    fleet_machine_profile,
    fleet_route,
    fleet_roster,
    fleet_runtime,
    fleet_sync_scheduler,
    machine_boot,
)
from tools.network.fleet_enrollment_client import (
    EnrollmentRecovery,
    EnrollmentResult,
    FleetJoinStateStore,
)
from tools.network.idkit import KeyPair, Subject, issue_cert


NOW_MS = 1_800_000_000_000
TOKEN = "45" * 16
TARGET_UUID = str(uuid.UUID("12345678-1234-5678-9234-567812345678"))
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def operator_api(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db(
        "machine", type_="personal", path=tmp_path / "machine.db"
    ).close()
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approvals.db")
    root = KeyPair.from_private_hex("34" * 32)
    invite = fleet_invite.mint(
        root,
        rendezvous=f"https://relay.auto.network/l/{TOKEN}",
        invite_id="56" * 32,
        expires_at=NOW_MS + 60_000,
    )
    store = fleet_enrollment_service.FleetEnrollmentStore(
        tmp_path / "machine.db"
    )
    grant = {
        "token": TOKEN,
        "target_type": "fleet:join",
        "target_uuid": TARGET_UUID,
    }
    monkeypatch.setattr(
        unlock_routes, "session_from_request", lambda _request: object()
    )
    monkeypatch.setattr(
        identity_routes,
        "_personal_member",
        lambda: SimpleNamespace(payload={"root_pub": root.public_hex}),
    )
    monkeypatch.setattr(
        link_serving,
        "check_grant",
        lambda token, org=None, now=None: grant if token == TOKEN else None,
    )
    monkeypatch.setattr(
        fleet_enrollment_service, "FleetEnrollmentStore", lambda: store
    )
    monkeypatch.setattr(
        fleet_enrollment_routes.fleet_relay_sync.dashboard_relay_sync_service,
        "configure",
        lambda _credential: None,
    )
    monkeypatch.setattr(
        fleet_enrollment_routes.fleet_relay_sync,
        "publish_connector_runtime",
        lambda _payload, org=None: None,
    )
    monkeypatch.setattr(
        fleet_enrollment_routes,
        "_ensure_fleet_catalog",
        lambda _machine_pub: None,
    )
    client = TestClient(Starlette(routes=[
        *fleet_enrollment_routes.ROUTES,
        *approvals_routes.ROUTES,
    ]))
    yield client, root, invite, store
    client.close()
    GraphDB.close_all_pooled()


def _register(client: TestClient, invite: fleet_invite.FleetInvite):
    response = client.post(
        "/api/fleet/invitations/register",
        json={
            "org": "autonomy",
            "target_uuid": TARGET_UUID,
            "grant_token": TOKEN,
            "invite": invite.to_dict(),
        },
    )
    assert response.status_code == 200, response.text


def test_generic_approval_commits_exact_request(operator_api, monkeypatch):
    client, root, invite, store = operator_api
    _register(client, invite)
    request = fleet_enroll.build_request(
        invite=invite, machine_id="78" * 32
    )
    channel = {}
    opened = fleet_enrollment_service.handle_request(
        {"target_type": "fleet:join", "target_uuid": TARGET_UUID},
        {"v": 1, "op": "fleet.request", "request": request.to_dict()},
        channel_state=channel,
        store=store,
        now_ms=NOW_MS,
    )
    pending = store.get_request(opened["request_id"])
    assert pending is not None
    approval_row = ar.get(pending.source_approval_id)
    assert approval_row is not None
    bootstrap_id = approval_row["staged"]["local_bootstrap_machine_id"]
    assert bootstrap_id is not None
    resume_token = opened["resume_token"]
    assert pending.source_approval_id == fleet_enrollment_approvals.approval_id_for(
        pending.request_id
    )

    listed = client.get(
        "/api/fleet/enrollment/requests",
        params={"target_uuid": TARGET_UUID},
    )
    assert listed.status_code == 200
    assert listed.json()["requests"] == [{
        "request_id": pending.request_id,
        "target_uuid": TARGET_UUID,
        "request": request.to_dict(),
        "channel_binding": pending.channel_binding,
        "verification_code": pending.verification_code,
        "status": "pending",
        "source_approval_id": pending.source_approval_id,
        "last_error_code": None,
        "created_at": NOW_MS,
        "updated_at": NOW_MS,
    }]

    root_seed = bytes.fromhex(root.private_hex)
    machine_id = fleet_enroll.assigned_machine_id(request)
    machine_key = fleet_enroll.derive_machine_key(root_seed, machine_id)
    roster_entry = fleet_roster.enroll(
        root,
        machine_id=machine_id,
        machine_pub=machine_key.public_hex,
        issued_at=NOW_MS,
    )
    draft = fleet_enroll.approval_draft(
        request,
        invite=invite,
        channel_binding=pending.channel_binding,
        roster_entry=roster_entry,
    )
    approval = fleet_enroll.EnrollmentApproval(
        **{
            **draft.__dict__,
            "signature": root.sign_hex(draft.signing_input()),
        }
    )
    local_key = fleet_enroll.derive_machine_key(root_seed, bootstrap_id)
    local_roster_entry = fleet_roster.enroll(
        root,
        machine_id=bootstrap_id,
        machine_pub=local_key.public_hex,
        issued_at=NOW_MS,
    )
    local_process = KeyPair.from_private_hex("82" * 32)
    local_cert = issue_cert(
        local_key,
        local_process.public_hex,
        scope=["fleet:sync"],
        org=f"personal:{root.public_hex}",
        subject=Subject(kind="machine", id=bootstrap_id),
        not_before=int(time.time()) - 30,
        not_after=int(time.time()) + 300,
    )
    local_runtime = {
        "machine_id": bootstrap_id,
        "machine_pub": local_key.public_hex,
        "process_private_seed": local_process.private_hex,
        "delegation_cert": local_cert.to_dict(),
    }
    approved = client.post(
        f"/api/approvals/{pending.source_approval_id}/decision",
        json={
            "approved": True,
            "machine_name": "SJC dashboard",
            "approval": approval.to_dict(),
            "roster_entry": roster_entry.to_dict(),
            "local_roster_entry": local_roster_entry.to_dict(),
            "local_runtime": local_runtime,
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json() == {"ok": True}
    result = client.get(
        f"/api/approvals/{pending.source_approval_id}", params={"wait": 5}
    ).json()["result"]
    assert result["approved"] is True
    assert result["execution"] == {
        "ok": True,
        "request_id": pending.request_id,
        "roster_entry_id": roster_entry.entry_id,
    }
    assert {entry.entry_id for entry in fleet_roster.load_entries(org=None)} == {
        local_roster_entry.entry_id,
        roster_entry.entry_id,
    }
    assert machine_boot.machine_id(org="machine") == bootstrap_id
    assert fleet_machine_profile.names(org=None) == {
        machine_id: "SJC dashboard"
    }
    resumed = store.resume(
        target_uuid=TARGET_UUID,
        request_id=pending.request_id,
        resume_token=resume_token,
        now_ms=NOW_MS + 1,
    )
    assert resumed.approval == approval

    # The joining browser receives only public evidence, derives the same
    # machine key locally, and proves it before machine.db gets an identity.
    join_state = FleetJoinStateStore(store.path.parent / "joining-machine.db")
    recovery = EnrollmentRecovery(
        invite=invite,
        request=request,
        request_id=pending.request_id,
        resume_token=resume_token,
        verification_code=pending.verification_code,
    )
    join_state.save(recovery, now_ms=NOW_MS)
    join_state.save_delivery(
        recovery.request_id,
        fleet_enroll.EnrollmentDelivery(
            approval,
            roster_entry,
            local_roster_entry,
        ),
        now_ms=NOW_MS,
    )
    monkeypatch.setattr(
        fleet_enrollment_routes,
        "_local_completion_state",
        lambda: (join_state, recovery, join_state.load_delivery(recovery.request_id)),
    )
    joining_identity = {}
    monkeypatch.setattr(
        machine_boot,
        "_read_row",
        lambda *, org: (
            {"machine_id": joining_identity["machine_id"]}
            if "machine_id" in joining_identity
            else None
        ),
    )
    monkeypatch.setattr(
        machine_boot,
        "_write_row",
        lambda payload, *, org: joining_identity.update(payload),
    )
    context = client.get("/api/fleet/enrollment/local-completion")
    assert context.status_code == 200
    assert context.json()["request_id"] == recovery.request_id
    proof = machine_key.sign_hex(machine_boot.completion_input(
        request_id=recovery.request_id,
        roster_entry_id=roster_entry.entry_id,
    ))
    process = KeyPair.from_private_hex("81" * 32)
    runtime_cert = issue_cert(
        machine_key,
        process.public_hex,
        scope=["fleet:sync"],
        org=f"personal:{root.public_hex}",
        subject=Subject(kind="machine", id=machine_id),
        not_before=int(time.time()) - 30,
        not_after=int(time.time()) + 300,
    )
    runtime_payload = {
        "machine_id": machine_id,
        "machine_pub": machine_key.public_hex,
        "process_private_seed": process.private_hex,
        "delegation_cert": runtime_cert.to_dict(),
    }
    configured = {}
    monkeypatch.setattr(
        fleet_enrollment_routes,
        "_runtime_context",
        lambda: (root.public_hex, roster_entry),
    )
    monkeypatch.setattr(
        fleet_sync_scheduler,
        "configure_dashboard_fleet_sync",
        lambda config: configured.update(config=config),
    )
    # The machine-local fleet-direct row decides where the direct listener
    # binds and what it advertises; activation must read it.
    from tools.network import fleet_direct_config

    fleet_direct_config.store(fleet_direct_config.FleetDirectConfig(
        "0.0.0.0", 9410, ("wss://sjc.example:9410",), advertise_auto=False,
    ))
    completed = client.post(
        "/api/fleet/enrollment/local-completion",
        json={
            "request_id": recovery.request_id,
            "machine_id": machine_id,
            "proof": proof,
            "runtime": runtime_payload,
        },
    )
    assert completed.status_code == 200, completed.text
    assert joining_identity == {"machine_id": machine_id}
    assert fleet_route.load(org="machine") == fleet_route.FleetRoute(
        invite.rendezvous, local_roster_entry.machine_pub
    )
    assert join_state.latest_any() is None
    assert resumed.roster_entry == roster_entry
    assert configured["config"].machine_key.private_hex == process.private_hex
    assert configured["config"].roster_machine_pub == machine_key.public_hex
    assert configured["config"].require_delegation is True
    assert configured["config"].listen_host == "0.0.0.0"
    assert configured["config"].listen_port == 9410
    assert fleet_enrollment_routes._reachability_cache.advertised_addrs() == [
        "wss://sjc.example:9410"
    ]

    # The same process-only handoff is reminted after every later unlock.
    runtime_context = client.get("/api/fleet/runtime")
    _tunnel = fleet_enrollment_routes.fleet_tunnel_server.state()
    assert runtime_context.json() == {
        "ok": True,
        "enabled": True,
        "personal_root_pub": root.public_hex,
        "machine_id": machine_id,
        "machine_pub": machine_key.public_hex,
        "org_uuid": None,  # personal org not registered in this fixture
        # The deterministic org_uuid the browser registers the personal tunnel
        # under, and whether this machine is the selected tunnel server.
        "personal_org_uuid": fleet_runtime.personal_org_uuid(root.public_hex),
        "serves": bool(
            _tunnel.allowed and _tunnel.selected_machine_id == machine_id
        ),
    }
    activated = client.post("/api/fleet/runtime", json=runtime_payload)
    assert activated.status_code == 200, activated.text
    assert activated.json() == {"ok": True, "machine_id": machine_id}


def test_operator_can_decline_and_agent_bearer_cannot_act(
    operator_api, monkeypatch
):
    client, _root, invite, store = operator_api
    _register(client, invite)
    request = fleet_enroll.build_request(
        invite=invite, machine_id="9a" * 32
    )
    opened = fleet_enrollment_service.handle_request(
        {"target_type": "fleet:join", "target_uuid": TARGET_UUID},
        {"v": 1, "op": "fleet.request", "request": request.to_dict()},
        channel_state={},
        store=store,
        now_ms=NOW_MS,
    )
    pending = store.get_request(opened["request_id"])
    assert pending is not None
    declined = client.post(
        f"/api/approvals/{pending.source_approval_id}/decision",
        json={"approved": False},
    )
    assert declined.status_code == 200
    assert declined.json() == {"ok": True}
    # Fleet does not copy the human verdict into its transport table.
    assert store.get_request(pending.request_id).status == "pending"
    resumed = fleet_enrollment_service.handle_request(
        {"target_type": "fleet:join", "target_uuid": TARGET_UUID},
        {
            "v": 1,
            "op": "fleet.resume",
            "request_id": pending.request_id,
            "resume_token": opened["resume_token"],
        },
        channel_state={},
        store=store,
        now_ms=NOW_MS + 1,
    )
    assert resumed["status"] == "declined"

    monkeypatch.setattr(
        unlock_routes, "session_from_request", lambda _request: None
    )
    denied = client.post(
        "/api/fleet/invitations/register",
        headers={"Authorization": "Bearer agent-session-token"},
        json={
            "org": "autonomy",
            "target_uuid": TARGET_UUID,
            "grant_token": TOKEN,
            "invite": invite.to_dict(),
        },
    )
    assert denied.status_code == 401
    assert "human Dashboard session" in denied.json()["error"]

    # There is exactly one human-decision path: the generic approval
    # rendezvous. The former Fleet-local decision routes do not exist.
    assert client.post(
        f"/api/fleet/enrollment/requests/{pending.request_id}/approve",
        json={},
    ).status_code == 404


def test_joining_screen_resumes_approved_delivery_without_node_restart(
    operator_api, monkeypatch
):
    client, root, invite, _store = operator_api
    request = fleet_enroll.build_request(
        invite=invite, machine_id="9b" * 32
    )
    recovery = EnrollmentRecovery(
        invite=invite,
        request=request,
        request_id=fleet_enroll.request_id(request),
        resume_token="ac" * 32,
        verification_code=fleet_enroll.verification_code(request),
    )
    machine_id = fleet_enroll.assigned_machine_id(request)
    machine_key = fleet_enroll.derive_machine_key(
        bytes.fromhex(root.private_hex), machine_id
    )
    roster = fleet_roster.enroll(
        root, machine_id=machine_id, machine_pub=machine_key.public_hex,
    )
    draft = fleet_enroll.approval_draft(
        request,
        invite=invite,
        channel_binding=recovery.channel_binding,
        roster_entry=roster,
    )
    approval = fleet_enroll.EnrollmentApproval(
        **{**draft.__dict__, "signature": root.sign_hex(draft.signing_input())}
    )
    delivery = fleet_enroll.EnrollmentDelivery(approval, roster)

    class State:
        saved = None

        def latest_any(self):
            return recovery

        def load_delivery(self, _request_id):
            return self.saved

        def save_delivery(self, request_id, value):
            assert request_id == recovery.request_id
            self.saved = value

    state = State()

    class Client:
        def __init__(self, *, state_store):
            assert state_store is state

        async def resume(self, value):
            assert value == recovery
            return EnrollmentResult(
                status="approved",
                recovery=recovery,
                delivery=delivery,
                personal_root_armor="encrypted-personal-armor",
                personal_root_created_at="2026-08-20T01:02:03Z",
                personal_root_updated_at="2026-08-24T04:05:06Z",
            )

    from tools.init import first_run
    from tools.network import fleet_enrollment_client

    stored = {}
    monkeypatch.setattr(fleet_enrollment_routes, "FleetJoinStateStore", lambda: state)
    monkeypatch.setattr(fleet_enrollment_client, "FleetEnrollmentClient", Client)
    monkeypatch.setattr(
        first_run,
        "_store_fleet_personal_armor",
        lambda armor, *, expected_root_pub, source_created_at,
               source_updated_at: stored.update({
            "armor": armor, "root_pub": expected_root_pub,
            "created_at": source_created_at, "updated_at": source_updated_at,
        }),
    )
    monkeypatch.setattr(
        unlock_routes, "bust_enforce_cache", lambda: stored.update({"busted": True})
    )

    response = client.post("/api/fleet/enrollment/local-resume")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "approved"}
    assert state.saved == delivery
    assert stored == {
        "armor": "encrypted-personal-armor",
        "root_pub": invite.personal_root_pub,
        "created_at": "2026-08-20T01:02:03Z",
        "updated_at": "2026-08-24T04:05:06Z",
        "busted": True,
    }


def test_invalid_remote_evidence_cannot_bootstrap_legacy_origin(
    operator_api, monkeypatch
):
    client, root, invite, store = operator_api
    store.register_invite(
        target_uuid=TARGET_UUID,
        grant_token=TOKEN,
        invite=invite,
        now_ms=NOW_MS,
    )
    opened = fleet_enrollment_service.handle_request(
        {"target_type": "fleet:join", "target_uuid": TARGET_UUID},
        {
            "v": 1,
            "op": "fleet.request",
            "request": fleet_enroll.build_request(
                invite=invite, machine_id="bb" * 32
            ).to_dict(),
        },
        channel_state={},
        store=store,
        now_ms=NOW_MS,
    )
    pending = store.get_request(opened["request_id"])
    assert pending is not None
    row = ar.get(pending.source_approval_id)
    bootstrap_id = row["staged"]["local_bootstrap_machine_id"]
    machine_id = fleet_enroll.assigned_machine_id(pending.request)
    machine_key = fleet_enroll.derive_machine_key(
        bytes.fromhex(root.private_hex), machine_id
    )
    remote_entry = fleet_roster.enroll(
        root, machine_id=machine_id, machine_pub=machine_key.public_hex,
    )
    draft = fleet_enroll.approval_draft(
        pending.request,
        invite=invite,
        channel_binding=pending.channel_binding,
        roster_entry=remote_entry,
    )
    invalid_approval = fleet_enroll.EnrollmentApproval(
        **{**draft.__dict__, "signature": "00" * 64}
    )
    local_key = fleet_enroll.derive_machine_key(
        bytes.fromhex(root.private_hex), bootstrap_id
    )
    local_entry = fleet_roster.enroll(
        root,
        machine_id=bootstrap_id,
        machine_pub=local_key.public_hex,
    )
    local_process = KeyPair.from_private_hex("83" * 32)
    local_cert = issue_cert(
        local_key,
        local_process.public_hex,
        scope=["fleet:sync"],
        org=f"personal:{root.public_hex}",
        subject=Subject(kind="machine", id=bootstrap_id),
        not_before=int(time.time()) - 30,
        not_after=int(time.time()) + 300,
    )

    response = client.post(
        f"/api/approvals/{pending.source_approval_id}/decision",
        json={
            "approved": True,
            "machine_name": "SJC dashboard",
            "approval": invalid_approval.to_dict(),
            "roster_entry": remote_entry.to_dict(),
            "local_roster_entry": local_entry.to_dict(),
            "local_runtime": {
                "machine_id": bootstrap_id,
                "machine_pub": local_key.public_hex,
                "process_private_seed": local_process.private_hex,
                "delegation_cert": local_cert.to_dict(),
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    result = client.get(
        f"/api/approvals/{pending.source_approval_id}", params={"wait": 5}
    ).json()["result"]
    assert result["execution"]["ok"] is False
    assert machine_boot.machine_id(org="machine") is None
    assert fleet_roster.load_entries(org=None) == []


def test_preapproval_table_is_forward_migrated(tmp_path):
    path = tmp_path / "machine.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE fleet_enrollment_pending ("
            "request_id TEXT PRIMARY KEY, target_uuid TEXT NOT NULL, "
            "request_json TEXT NOT NULL, channel_binding TEXT NOT NULL, "
            "verification_code TEXT NOT NULL, status TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
    fleet_enrollment_service.FleetEnrollmentStore(path)
    with sqlite3.connect(path) as conn:
        columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(fleet_enrollment_pending)"
            )
        }
    assert {
        "approval_json", "roster_entry_json", "source_approval_id",
        "last_error_code",
    } <= columns


def test_local_sync_status_waits_for_current_roster_checkpoint(
    operator_api, monkeypatch
):
    client, root, _invite, store = operator_api
    local_id = "a1" * 32
    remote_id = "b2" * 32
    local_key = KeyPair.from_private_hex("71" * 32)
    remote_key = KeyPair.from_private_hex("72" * 32)
    entries = (
        fleet_roster.enroll(
            root, machine_id=local_id, machine_pub=local_key.public_hex,
        ),
        fleet_roster.enroll(
            root, machine_id=remote_id, machine_pub=remote_key.public_hex,
        ),
    )
    personal_path = store.path.parent / "sync-personal.db"
    epoch = fleet_enrollment_routes.fleet_sync_scheduler.roster_epoch(
        entries, root.public_hex
    )
    with sqlite3.connect(personal_path) as conn:
        conn.execute(
            "CREATE TABLE fleet_sync_peer_state("
            "machine_public_key TEXT, roster_epoch TEXT, "
            "checkpoints_received INTEGER, last_success_ns INTEGER)"
        )
    monkeypatch.setattr(
        fleet_enrollment_routes.machine_boot,
        "machine_id",
        lambda *, org: local_id,
    )
    monkeypatch.setattr(
        fleet_enrollment_routes.fleet_tunnel_server,
        "_personal_root_pub",
        lambda: root.public_hex,
    )
    monkeypatch.setattr(
        fleet_enrollment_routes.fleet_roster,
        "load_entries",
        lambda *, org: list(entries),
    )
    monkeypatch.setattr(
        fleet_enrollment_routes, "_org_db_path", lambda _org: personal_path
    )

    waiting = client.get("/api/fleet/enrollment/local-sync-status")
    assert waiting.status_code == 200
    assert waiting.json() == {"ok": True, "status": "synchronizing"}

    with sqlite3.connect(personal_path) as conn:
        conn.execute(
            "INSERT INTO fleet_sync_peer_state VALUES(?,?,1,?)",
            (remote_key.public_hex, epoch, NOW_MS * 1_000_000),
        )
    complete = client.get("/api/fleet/enrollment/local-sync-status")
    assert complete.status_code == 200
    assert complete.json() == {"ok": True, "status": "complete"}


def test_current_generic_dialogue_owns_pin_and_browser_root_ceremony():
    js = (REPO_ROOT / "tools/dashboard/static/js/pages/worktrees.js").read_text()
    template = (
        REPO_ROOT
        / "tools/dashboard/templates/partials/worktree-review-overlays.html"
    ).read_text()
    assert "fleet_machine_admission:" in js
    assert "mintFleetEnrollmentEvidence" in js
    assert "roster_entry: evidence.rosterEntry" in js
    assert "decision.local_roster_entry = evidence.localRosterEntry" in js
    assert "decision.local_runtime = evidence.localRuntime" in js
    assert "machine_name: machineName" in js
    assert "approval-fleet-machine-name" in template
    assert "approval-fleet-pin" in template
    assert "Machine comparison code" in template
    assert "/api/fleet/enrollment/requests/" not in js


def test_deactivate_invitation_stops_new_requests_locally(operator_api):
    client, _root, invite, store = operator_api
    _register(client, invite)
    assert store.current_invitation(now_ms=NOW_MS) is not None

    response = client.post(
        "/api/fleet/invitations/deactivate",
        json={"target_uuid": TARGET_UUID},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert store.current_invitation(now_ms=NOW_MS) is None

    # Idempotence is refused loudly: a second deactivation names the miss.
    repeat = client.post(
        "/api/fleet/invitations/deactivate",
        json={"target_uuid": TARGET_UUID},
    )
    assert repeat.status_code == 404


def test_deactivate_invitation_requires_exact_body(operator_api):
    client, _root, _invite, _store = operator_api
    response = client.post(
        "/api/fleet/invitations/deactivate",
        json={"target_uuid": TARGET_UUID, "extra": 1},
    )
    assert response.status_code == 400
