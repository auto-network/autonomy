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


def _runtime_serves(client, monkeypatch, *, allowed, reason):
    """Read `serves` off /api/fleet/runtime with the election forced."""
    from tools.network import fleet_tunnel_server as fts

    entry = SimpleNamespace(machine_id="local-machine", machine_pub="bb" * 32)
    monkeypatch.setattr(
        fleet_enrollment_routes, "_runtime_context", lambda: ("aa" * 32, entry))
    monkeypatch.setattr(fleet_enrollment_routes, "_reachability_binding", lambda: None)
    monkeypatch.setattr(fts, "state", lambda: SimpleNamespace(
        allowed=allowed, reason=reason, managed=True,
        selected_machine_id="other-machine"))
    body = client.get("/api/fleet/runtime").json()
    return body["serves"]


def test_a_non_designated_machine_is_told_to_mint_its_serving_cert(
    operator_api, monkeypatch
):
    """THE ONE THAT MATTERS for SJC. `serves` is the browser's mint trigger, and
    the key material for that mint exists only in the operator's browser during
    unlock — so a machine told False here never acquires a personal serving
    certificate, and relaxing the supervisor's launch gate alone leaves it with
    nothing to launch. Observed on SJC: personal serving cert missing, no local
    serve artifact, while the machine is rostered and healthy.

    The election itself stays singular: `allowed` is False in this test."""
    assert _runtime_serves(
        operator_api[0], monkeypatch, allowed=False, reason="not-designated") is True


def test_a_mid_join_machine_is_still_not_told_to_serve(operator_api, monkeypatch):
    """NEGATIVE CONTROL. The old expression protected this by way of
    designation; the predicate must protect it directly, or a machine would mint
    a serving delegate before its roster has arrived."""
    assert _runtime_serves(
        operator_api[0], monkeypatch, allowed=False,
        reason="fleet-member-provisioning") is False


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
