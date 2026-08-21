"""The machine-neutral fleet request over the real RelayKit handler seam."""

from __future__ import annotations

import json
import uuid

import pytest

from tools.dashboard import fleet_enrollment_service, link_approvals, link_serving
from tools.graph.db import GraphDB
from tools.graph.schemas import network_identity
from tools.graph.schemas.registry import validate_payload
from tools.network import fleet_enroll, fleet_invite, fleet_roster
from tools.network.idkit import KeyPair


NOW_MS = 1_777_000_000_000
NOW_S = NOW_MS / 1000
TOKEN = "12" * 16


@pytest.fixture
def rendezvous(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    root = KeyPair.from_private_hex(("34" * 32))
    target_uuid = str(uuid.UUID("12345678-1234-5678-9234-567812345678"))
    invite = fleet_invite.mint(
        root,
        rendezvous=f"https://relay.auto.network/l/{TOKEN}",
        invite_id="56" * 32,
        expires_at=NOW_MS + 60_000,
    )
    store = fleet_enrollment_service.FleetEnrollmentStore(
        tmp_path / "machine.db"
    )
    store.register_invite(
        target_uuid=target_uuid,
        grant_token=TOKEN,
        invite=invite,
        now_ms=NOW_MS,
    )
    grant = {
        "target_type": "fleet:join",
        "target_uuid": target_uuid,
    }
    request = fleet_enroll.build_request(
        invite=invite,
        enrollment_nonce="78" * 32,
    )
    yield root, invite, store, grant, request
    GraphDB.close_all_pooled()


def test_new_request_gets_one_ephemeral_resume_token(rendezvous):
    _root, _invite, store, grant, request = rendezvous
    channel = {}
    reply = fleet_enrollment_service.handle_request(
        grant,
        {"v": 1, "op": "fleet.request", "request": request.to_dict()},
        channel_state=channel,
        store=store,
        now_ms=NOW_MS,
    )
    assert reply["status"] == "pending"
    assert reply["request_id"] == fleet_enroll.request_id(request)
    assert reply["verification_code"] == fleet_enroll.verification_code(request)
    assert len(reply["resume_token"]) == 64
    assert set(request.to_dict()) == {
        "enrollment_nonce", "personal_root_pub", "invite_id"
    }

    pending = store.list_pending(grant["target_uuid"])
    assert len(pending) == 1
    assert pending[0].channel_binding == (
        fleet_enrollment_service.channel_binding(reply["resume_token"])
    )
    raw_db = store.path.read_bytes()
    assert reply["resume_token"].encode() not in raw_db


def test_same_channel_retries_but_another_channel_must_resume(rendezvous):
    _root, _invite, store, grant, request = rendezvous
    message = {"v": 1, "op": "fleet.request", "request": request.to_dict()}
    first_channel = {}
    first = fleet_enrollment_service.handle_request(
        grant, message, channel_state=first_channel, store=store, now_ms=NOW_MS
    )
    repeated = fleet_enrollment_service.handle_request(
        grant, message, channel_state=first_channel, store=store, now_ms=NOW_MS
    )
    assert repeated == first

    other_channel = {}
    duplicate = fleet_enrollment_service.handle_request(
        grant, message, channel_state=other_channel, store=store, now_ms=NOW_MS
    )
    assert duplicate == {"v": 1, "status": "resume-required"}

    resumed = fleet_enrollment_service.handle_request(
        grant,
        {
            "v": 1,
            "op": "fleet.resume",
            "request_id": first["request_id"],
            "resume_token": first["resume_token"],
        },
        channel_state=other_channel,
        store=store,
        now_ms=NOW_MS,
    )
    assert resumed == {
        "v": 1,
        "status": "pending",
        "request_id": first["request_id"],
        "verification_code": first["verification_code"],
    }


def test_multiple_requests_stay_distinct_and_wrong_channel_fails(rendezvous):
    _root, invite, store, grant, first_request = rendezvous
    first = fleet_enrollment_service.handle_request(
        grant,
        {"v": 1, "op": "fleet.request", "request": first_request.to_dict()},
        channel_state={},
        store=store,
        now_ms=NOW_MS,
    )
    second_request = fleet_enroll.build_request(
        invite=invite,
        enrollment_nonce="9a" * 32,
    )
    second = fleet_enrollment_service.handle_request(
        grant,
        {"v": 1, "op": "fleet.request", "request": second_request.to_dict()},
        channel_state={},
        store=store,
        now_ms=NOW_MS,
    )
    assert first["request_id"] != second["request_id"]
    assert first["verification_code"] != second["verification_code"]
    assert len(store.list_pending(grant["target_uuid"])) == 2

    with pytest.raises(
        fleet_enrollment_service.FleetEnrollmentChannelError,
        match="unknown fleet enrollment request or channel",
    ):
        store.resume(
            target_uuid=grant["target_uuid"],
            request_id=first["request_id"],
            resume_token=second["resume_token"],
            now_ms=NOW_MS,
        )


def test_approval_commits_roster_before_resume_delivers_unchanged_armor(
    rendezvous,
):
    root, invite, store, grant, request = rendezvous
    channel = {}
    first = fleet_enrollment_service.handle_request(
        grant,
        {"v": 1, "op": "fleet.request", "request": request.to_dict()},
        channel_state=channel,
        store=store,
        now_ms=NOW_MS,
    )
    pending = store.list_pending(grant["target_uuid"])[0]
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
    approved = store.approve(
        target_uuid=grant["target_uuid"],
        request_id=first["request_id"],
        approval=approval,
        roster_entry=roster_entry,
        anchor_root_pub=root.public_hex,
        org=None,
        now_ms=NOW_MS + 1,
    )
    assert approved.status == "approved"
    assert [entry.entry_id for entry in fleet_roster.load_entries(org=None)] == [
        roster_entry.entry_id
    ]

    armor = "UNCHANGED-PASSWORD-ENCRYPTED-ARMOR"
    delivered = fleet_enrollment_service.handle_request(
        grant,
        {
            "v": 1,
            "op": "fleet.resume",
            "request_id": first["request_id"],
            "resume_token": first["resume_token"],
        },
        channel_state={},
        store=store,
        armor_provider=lambda: (armor, root.public_hex),
        now_ms=NOW_MS + 2,
    )
    assert delivered["status"] == "approved"
    assert delivered["approval"] == approval.to_dict()
    assert delivered["roster_entry"] == roster_entry.to_dict()
    assert delivered["personal_root_armor"] == armor

    # Retrying approval is idempotent: the content-addressed roster key is
    # updated in place rather than adding another base row.
    store.approve(
        target_uuid=grant["target_uuid"],
        request_id=first["request_id"],
        approval=approval,
        roster_entry=roster_entry,
        anchor_root_pub=root.public_hex,
        org=None,
        now_ms=NOW_MS + 3,
    )
    assert len(fleet_roster.load_entries(org=None)) == 1

    competing_entry = fleet_roster.enroll(
        root,
        machine_id=machine_id,
        machine_pub=machine_key.public_hex,
        seq=1,
        issued_at=NOW_MS + 4,
    )
    competing_draft = fleet_enroll.approval_draft(
        request,
        invite=invite,
        channel_binding=pending.channel_binding,
        roster_entry=competing_entry,
    )
    competing_approval = fleet_enroll.EnrollmentApproval(
        **{
            **competing_draft.__dict__,
            "signature": root.sign_hex(competing_draft.signing_input()),
        }
    )
    with pytest.raises(
        fleet_enrollment_service.FleetEnrollmentChannelError,
        match="different approval evidence",
    ):
        store.approve(
            target_uuid=grant["target_uuid"],
            request_id=first["request_id"],
            approval=competing_approval,
            roster_entry=competing_entry,
            anchor_root_pub=root.public_hex,
            org=None,
            now_ms=NOW_MS + 5,
        )
    assert len(fleet_roster.load_entries(org=None)) == 1


def test_expired_or_rebound_invite_refuses(rendezvous):
    _root, invite, store, grant, request = rendezvous
    with pytest.raises(
        fleet_enrollment_service.FleetEnrollmentChannelError,
        match="expired",
    ):
        store.open_request(
            grant["target_uuid"], request, now_ms=invite.expires_at
        )
    with pytest.raises(
        fleet_enrollment_service.FleetEnrollmentChannelError,
        match="different bytes",
    ):
        store.register_invite(
            target_uuid=grant["target_uuid"],
            grant_token="ab" * 16,
            invite=fleet_invite.mint(
                KeyPair.from_private_hex("34" * 32),
                rendezvous=f"https://relay.auto.network/l/{'ab' * 16}",
                invite_id=invite.invite_id,
                expires_at=invite.expires_at,
            ),
            now_ms=NOW_MS,
        )


@pytest.mark.asyncio
async def test_real_grant_handler_keeps_channel_state(
    rendezvous, monkeypatch
):
    _root, _invite, store, grant, request = rendezvous
    full_grant = {**grant, "token": TOKEN}
    monkeypatch.setattr(
        link_serving,
        "check_grant",
        lambda token, org=None, now=None: full_grant if token == TOKEN else None,
    )
    monkeypatch.setattr(
        fleet_enrollment_service, "FleetEnrollmentStore", lambda: store
    )
    handler = link_serving.make_grant_handler("autonomy", now=lambda: NOW_S)
    channel = await handler.for_channel(TOKEN)
    message = json.dumps({
        "v": 1, "op": "fleet.request", "request": request.to_dict()
    }).encode()
    first = json.loads(await channel(TOKEN, message))
    assert first["status"] == "pending"
    assert json.loads(await channel(TOKEN, message)) == first

    second_channel = await handler.for_channel(TOKEN)
    assert json.loads(await second_channel(TOKEN, message)) == {
        "status": "resume-required", "v": 1
    }
    wrong = json.dumps({
        "v": 1,
        "op": "fleet.resume",
        "request_id": first["request_id"],
        "resume_token": "ff" * 32,
    }).encode()
    assert await second_channel(TOKEN, wrong) == link_serving.REFUSED


def test_link_grant_schema_and_approval_surface_name_fleet_join():
    payload = {
        "token": TOKEN,
        "target_uuid": "12345678-1234-5678-9234-567812345678",
        "target_type": "fleet:join",
        "meta": {"ttl": 300, "label": "SJC fleet install"},
        "subject": {"kind": "operator", "id": "operator-session"},
        "issued_at": "2026-08-21T18:00:00Z",
        "url": f"https://relay.auto.network/l/{TOKEN}",
    }
    validate_payload(
        network_identity.NETWORK_LINK_GRANT_SET_ID,
        network_identity.NETWORK_LINK_GRANT_REVISION,
        payload,
    )
    resolved = link_approvals._resolve_target(
        "fleet:join", payload["target_uuid"], "autonomy"
    )
    assert resolved == {"title": "Fleet machine invitation", "error": None}


def test_fleet_channel_rejects_unknown_protocol_version(rendezvous):
    _root, _invite, store, grant, request = rendezvous
    with pytest.raises(
        fleet_enrollment_service.FleetEnrollmentChannelError,
        match="unsupported fleet invitation protocol version",
    ):
        fleet_enrollment_service.handle_request(
            grant,
            {"v": 2, "op": "fleet.request", "request": request.to_dict()},
            channel_state={},
            store=store,
            now_ms=NOW_MS,
        )
