"""Operator API acceptance for the fleet invitation rendezvous."""

from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import (
    fleet_enrollment_routes,
    fleet_enrollment_service,
    identity_routes,
    link_serving,
    unlock_routes,
)
from tools.graph.db import GraphDB
from tools.network import fleet_enroll, fleet_invite, fleet_roster
from tools.network.idkit import KeyPair


NOW_MS = 1_800_000_000_000
TOKEN = "45" * 16
TARGET_UUID = str(uuid.UUID("12345678-1234-5678-9234-567812345678"))


@pytest.fixture
def operator_api(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
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
    client = TestClient(Starlette(routes=fleet_enrollment_routes.ROUTES))
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


def test_operator_registers_lists_and_approves_exact_request(operator_api):
    client, root, invite, store = operator_api
    _register(client, invite)
    request = fleet_enroll.build_request(
        invite=invite, enrollment_nonce="78" * 32
    )
    pending, resume_token = store.open_request(
        TARGET_UUID, request, now_ms=NOW_MS
    )
    assert resume_token is not None

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
        "created_at": NOW_MS,
        "updated_at": NOW_MS,
    }]

    root_seed = bytes.fromhex(root.private_hex)
    machine_id = fleet_enroll.assigned_machine_id(root_seed, request)
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
    approved = client.post(
        f"/api/fleet/enrollment/requests/{pending.request_id}/approve",
        json={
            "target_uuid": TARGET_UUID,
            "approval": approval.to_dict(),
            "roster_entry": roster_entry.to_dict(),
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json() == {
        "ok": True,
        "request_id": pending.request_id,
        "status": "approved",
        "roster_entry_id": roster_entry.entry_id,
    }
    assert fleet_roster.load_entries(org=None) == [roster_entry]
    resumed = store.resume(
        target_uuid=TARGET_UUID,
        request_id=pending.request_id,
        resume_token=resume_token,
        now_ms=NOW_MS + 1,
    )
    assert resumed.approval == approval
    assert resumed.roster_entry == roster_entry


def test_operator_can_decline_and_agent_bearer_cannot_act(
    operator_api, monkeypatch
):
    client, _root, invite, store = operator_api
    _register(client, invite)
    request = fleet_enroll.build_request(
        invite=invite, enrollment_nonce="9a" * 32
    )
    pending, _resume_token = store.open_request(
        TARGET_UUID, request, now_ms=NOW_MS
    )
    declined = client.post(
        f"/api/fleet/enrollment/requests/{pending.request_id}/decline",
        json={"target_uuid": TARGET_UUID},
    )
    assert declined.status_code == 200
    assert declined.json() == {"ok": True, "status": "declined"}
    assert store.list_pending(TARGET_UUID) == ()

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
    assert {"approval_json", "roster_entry_json"} <= columns
