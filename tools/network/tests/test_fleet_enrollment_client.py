"""Fresh-install request/resume client without pre-approval machine identity."""

from __future__ import annotations

import json
import uuid

import pytest

from tools.dashboard import fleet_enrollment_service
from tools.dashboard.dao import approval_requests as ar
from tools.graph.db import GraphDB
from tools.network import (
    fleet_enroll,
    fleet_enrollment_client,
    fleet_invite,
    fleet_roster,
)
from tools.network.idkit import KeyPair


NOW_MS = 1_800_000_000_000
TOKEN = "12" * 16
TARGET_UUID = str(uuid.UUID("12345678-1234-5678-9234-567812345678"))


class HandlerChannel:
    def __init__(self, handler):
        self.handler = handler
        self.message = None
        self.closed = False

    async def send_message(self, message):
        self.message = json.loads(message)

    async def recv_message(self):
        return json.dumps(self.handler(self.message)).encode()

    async def close(self):
        self.closed = True


@pytest.fixture
def path(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approvals.db")
    root = KeyPair.from_private_hex("34" * 32)
    serving = KeyPair.from_private_hex("56" * 32)
    invite = fleet_invite.mint(
        root,
        rendezvous=f"https://relay.auto.network/l/{TOKEN}",
        invite_id="78" * 32,
        expires_at=NOW_MS + 60_000,
    )
    origin = fleet_enrollment_service.FleetEnrollmentStore(
        tmp_path / "origin-machine.db"
    )
    origin.register_invite(
        target_uuid=TARGET_UUID,
        grant_token=TOKEN,
        invite=invite,
        now_ms=NOW_MS,
    )
    grant = {"target_type": "fleet:join", "target_uuid": TARGET_UUID}
    envelope = {
        "org": "autonomy",
        "root_pub": serving.public_hex,
        "target_type": "fleet:join",
        "target_uuid": TARGET_UUID,
    }
    channels = []

    async def fetch(base, token):
        assert base == "https://relay.auto.network"
        assert token == TOKEN
        return envelope

    async def connect(base, token, *, root_pub, org):
        assert (base, token) == ("wss://relay.auto.network", TOKEN)
        assert (root_pub, org) == (serving.public_hex, "autonomy")
        state = {}

        def handle(message):
            return fleet_enrollment_service.handle_request(
                grant,
                message,
                channel_state=state,
                store=origin,
                armor_provider=lambda: (
                    "UNCHANGED-PASSWORD-ENCRYPTED-ARMOR",
                    root.public_hex,
                ),
                now_ms=NOW_MS,
            )

        channel = HandlerChannel(handle)
        channels.append(channel)
        return channel

    join_store = fleet_enrollment_client.FleetJoinStateStore(
        tmp_path / "joining-machine.db"
    )
    client = fleet_enrollment_client.FleetEnrollmentClient(
        envelope_fetcher=fetch,
        channel_connector=connect,
        state_store=join_store,
        now_ms=lambda: NOW_MS,
    )
    yield root, invite, origin, join_store, client, channels
    GraphDB.close_all_pooled()


@pytest.mark.asyncio
async def test_start_persists_only_machine_local_recovery(path):
    _root, invite, origin, join_store, client, channels = path
    recovery = await client.start(invite, enrollment_nonce="9a" * 32)
    assert recovery.verification_code == fleet_enroll.verification_code(
        recovery.request
    )
    assert join_store.load(recovery.request_id) == recovery
    assert join_store.latest(invite.invite_id) == recovery
    assert origin.list_pending(TARGET_UUID)[0].channel_binding == (
        recovery.channel_binding
    )
    assert channels[0].closed is True
    recovered = await client.start_or_recover(
        invite, enrollment_nonce="ff" * 32
    )
    assert recovered == recovery
    assert len(origin.list_pending(TARGET_UUID)) == 1
    assert len(channels) == 1
    raw = join_store.path.read_bytes()
    assert b"machine_id" not in raw
    assert b"machine_pub" not in raw
    assert b"personal_root_armor" not in raw
    join_store.delete(recovery.request_id)
    assert join_store.load(recovery.request_id) is None


@pytest.mark.asyncio
async def test_resume_verifies_public_approval_and_returns_unchanged_armor(path):
    root, invite, origin, _join_store, client, _channels = path
    recovery = await client.start(invite, enrollment_nonce="9a" * 32)
    pending = await client.resume(recovery)
    assert pending.status == "pending"
    assert pending.delivery is None

    root_seed = bytes.fromhex(root.private_hex)
    machine_id = fleet_enroll.assigned_machine_id(root_seed, recovery.request)
    machine_key = fleet_enroll.derive_machine_key(root_seed, machine_id)
    roster_entry = fleet_roster.enroll(
        root,
        machine_id=machine_id,
        machine_pub=machine_key.public_hex,
        issued_at=NOW_MS,
    )
    draft = fleet_enroll.approval_draft(
        recovery.request,
        invite=invite,
        channel_binding=recovery.channel_binding,
        roster_entry=roster_entry,
    )
    approval = fleet_enroll.EnrollmentApproval(
        **{
            **draft.__dict__,
            "signature": root.sign_hex(draft.signing_input()),
        }
    )
    origin.approve(
        target_uuid=TARGET_UUID,
        request_id=recovery.request_id,
        approval=approval,
        roster_entry=roster_entry,
        anchor_root_pub=root.public_hex,
        org=None,
        now_ms=NOW_MS,
    )

    approved = await client.resume(recovery)
    assert approved.status == "approved"
    assert approved.delivery == fleet_enroll.EnrollmentDelivery(
        approval, roster_entry
    )
    assert approved.personal_root_armor == (
        "UNCHANGED-PASSWORD-ENCRYPTED-ARMOR"
    )


@pytest.mark.asyncio
async def test_wrong_envelope_type_and_tampered_recovery_fail_closed(path):
    _root, invite, _origin, join_store, client, _channels = path
    recovery = await client.start(invite, enrollment_nonce="9a" * 32)
    bad = recovery.to_dict()
    bad["request_id"] = "ff" * 32
    with pytest.raises(
        fleet_enrollment_client.FleetEnrollmentClientError,
        match="request id does not match",
    ):
        fleet_enrollment_client.EnrollmentRecovery.from_dict(bad)

    async def wrong_envelope(_base, _token):
        return {
            "org": "autonomy",
            "root_pub": "34" * 32,
            "target_type": "org:join",
            "target_uuid": TARGET_UUID,
        }

    refused = fleet_enrollment_client.FleetEnrollmentClient(
        envelope_fetcher=wrong_envelope,
        channel_connector=client.channel_connector,
        state_store=join_store,
        now_ms=lambda: NOW_MS,
    )
    with pytest.raises(
        fleet_enrollment_client.FleetEnrollmentClientError,
        match="not a fleet invitation",
    ):
        await refused.resume(recovery)
