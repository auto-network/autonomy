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
    machine_boot,
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
    orgs = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db(
        "machine", type_="personal", path=orgs.parent / "machine.db"
    ).close()
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approvals.db")
    root = KeyPair.from_private_hex("34" * 32)
    origin_machine_id = "01" * 32
    origin_key = fleet_enroll.derive_machine_key(
        bytes.fromhex(root.private_hex), origin_machine_id
    )
    fleet_roster.store_entry(
        fleet_roster.enroll(
            root,
            machine_id=origin_machine_id,
            machine_pub=origin_key.public_hex,
            issued_at=NOW_MS - 1,
        ),
        org=None,
    )
    # This fixture's "origin" is the already-enrolled Dashboard approving the
    # join -- it must know its own machine id directly, the same way a real
    # Dashboard does, so the server can hand over its own row without a scan.
    machine_boot._write_row({"machine_id": origin_machine_id}, org="machine")
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
                    "2026-08-20T01:02:03Z",
                    "2026-08-24T04:05:06Z",
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
    recovery = await client.start(invite, machine_id="9a" * 32)
    assert recovery.verification_code == fleet_enroll.verification_code(
        recovery.request
    )
    assert join_store.load(recovery.request_id) == recovery
    assert join_store.latest(invite.invite_id) == recovery
    assert join_store.latest_any() == recovery
    assert origin.list_pending(TARGET_UUID)[0].channel_binding == (
        recovery.channel_binding
    )
    assert channels[0].closed is True
    recovered = await client.start_or_recover(
        invite, machine_id="ff" * 32
    )
    assert recovered == recovery
    assert len(origin.list_pending(TARGET_UUID)) == 1
    assert len(channels) == 1
    raw = join_store.path.read_bytes()
    assert b"machine_id" in raw
    assert b"machine_pub" not in raw
    assert b"personal_root_armor" not in raw
    join_store.delete(recovery.request_id)
    assert join_store.load(recovery.request_id) is None
    assert join_store.latest_any() is None


@pytest.mark.asyncio
async def test_resume_verifies_public_approval_and_returns_unchanged_armor(path):
    root, invite, origin, join_store, client, _channels = path
    recovery = await client.start(invite, machine_id="9a" * 32)
    pending = await client.resume(recovery)
    assert pending.status == "pending"
    assert pending.delivery is None

    root_seed = bytes.fromhex(root.private_hex)
    machine_id = fleet_enroll.assigned_machine_id(recovery.request)
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
    assert approved.delivery.approval == approval
    assert approved.delivery.roster_entry == roster_entry
    assert approved.delivery.origin_entry is not None
    assert approved.delivery.origin_entry.machine_id == "01" * 32
    assert approved.delivery.origin_entry.machine_pub != roster_entry.machine_pub
    assert approved.personal_root_armor == (
        "UNCHANGED-PASSWORD-ENCRYPTED-ARMOR"
    )
    assert approved.personal_root_created_at == "2026-08-20T01:02:03Z"
    assert approved.personal_root_updated_at == "2026-08-24T04:05:06Z"
    join_store.save_delivery(recovery.request_id, approved.delivery)
    assert join_store.load_delivery(recovery.request_id) == approved.delivery
    raw = join_store.path.read_bytes()
    assert b"UNCHANGED-PASSWORD-ENCRYPTED-ARMOR" not in raw


@pytest.mark.asyncio
async def test_wrong_envelope_type_and_tampered_recovery_fail_closed(path):
    _root, invite, _origin, join_store, client, _channels = path
    recovery = await client.start(invite, machine_id="9a" * 32)
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


@pytest.mark.asyncio
async def test_expired_invite_discards_retry_state_without_reopening_channel(path):
    _root, invite, _origin, join_store, client, channels = path
    recovery = await client.start(invite, machine_id="9a" * 32)
    before = len(channels)
    expired_client = fleet_enrollment_client.FleetEnrollmentClient(
        envelope_fetcher=client.envelope_fetcher,
        channel_connector=client.channel_connector,
        state_store=join_store,
        now_ms=lambda: invite.expires_at,
    )

    result = await expired_client.resume(recovery)

    assert result.status == "expired"
    assert join_store.load(recovery.request_id) is None
    assert len(channels) == before


@pytest.mark.asyncio
async def test_resume_names_the_actionable_failure(path):
    """The three resume failures a joiner can act on are distinct messages,
    not one opaque "does not match" line (operator-reported 2026-09-04: a
    stale/offline serving Dashboard surfaced only the cryptic mismatch)."""
    _root, invite, _origin, join_store, client, _channels = path
    recovery = await client.start(invite, machine_id="9c" * 32)

    def _client_returning(reply):
        async def connect(_base, _token, *, root_pub, org):
            return HandlerChannel(lambda _message: reply)
        return fleet_enrollment_client.FleetEnrollmentClient(
            envelope_fetcher=client.envelope_fetcher,
            channel_connector=connect,
            state_store=join_store,
            now_ms=lambda: NOW_MS,
        )

    # Offline / locked / stale-code serving side: a gateway-shaped object,
    # not a fleet enrollment envelope.
    not_serving = _client_returning({"error": "bad gateway", "code": 502})
    with pytest.raises(
        fleet_enrollment_client.FleetEnrollmentClientError,
        match="did not return a valid enrollment response",
    ):
        await not_serving.resume(recovery)

    # Well-formed envelope, but for a different request id.
    wrong_request = _client_returning({
        "v": 1, "status": "pending",
        "request_id": "ff" * 32,
        "verification_code": recovery.verification_code,
    })
    with pytest.raises(
        fleet_enrollment_client.FleetEnrollmentClientError,
        match="different request",
    ):
        await wrong_request.resume(recovery)

    # Well-formed envelope, but the verification code differs (a different
    # invitation on the serving side).
    wrong_code = _client_returning({
        "v": 1, "status": "pending",
        "request_id": recovery.request_id,
        "verification_code": "00" * 16,
    })
    with pytest.raises(
        fleet_enrollment_client.FleetEnrollmentClientError,
        match="different invitation",
    ):
        await wrong_code.resume(recovery)
