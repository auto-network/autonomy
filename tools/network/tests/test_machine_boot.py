"""Joining-machine persistence starts only after a verified approval."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tools.graph.db import GraphDB
from tools.network import fleet_enroll, fleet_invite, fleet_roster, machine_boot
from tools.network.idkit import KeyPair, derive_machine_key
from tools.network.machine_boot import MachineBootError


CHANNEL = "ca" * 32


@pytest.fixture
def machine(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db(
        "machine", type_="personal", path=orgs.parent / "machine.db"
    ).close()
    yield tmp_path
    GraphDB.close_all_pooled()


def _invite():
    root = KeyPair.generate()
    return root, fleet_invite.mint(
        root,
        rendezvous="https://primary.example.net/rv/x",
        invite_id="ab" * 32,
    )


def _approved(root, invite, request):
    root_seed = bytes.fromhex(root.private_hex)
    machine_id = fleet_enroll.assigned_machine_id(root_seed, request)
    key = derive_machine_key(root_seed, machine_id)
    entry = fleet_roster.enroll(
        root,
        machine_id=machine_id,
        machine_pub=key.public_hex,
        assignment=fleet_roster.FLEET_MEMBER_ASSIGNMENT,
    )
    draft = fleet_enroll.approval_draft(
        request,
        invite=invite,
        channel_binding=CHANNEL,
        roster_entry=entry,
    )
    approval = replace(draft, signature=root.sign_hex(draft.signing_input()))
    return fleet_enroll.authorize_request(
        approval,
        request,
        invite=invite,
        channel_binding=CHANNEL,
        roster_entry=entry,
        anchor_root_pub=root.public_hex,
        org="personal",
    )


def test_first_boot_creates_only_ephemeral_request_and_no_identity(machine):
    _, invite = _invite()
    request, code = machine_boot.first_boot(invite)

    assert request.enrollment_nonce
    assert code == fleet_enroll.verification_code(request)
    assert machine_boot.has_identity() is False
    assert request.enrollment_nonce.encode() not in (machine / "machine.db").read_bytes()


def test_decline_or_expiry_leaves_no_machine_state(machine):
    _, invite = _invite()
    first, _ = machine_boot.first_boot(invite)
    second, _ = machine_boot.first_boot(invite)

    assert first.enrollment_nonce != second.enrollment_nonce
    assert machine_boot.has_identity() is False


def test_roster_commit_then_completion_stores_only_assigned_machine_id(machine):
    root, invite = _invite()
    request, _ = machine_boot.first_boot(invite)
    delivery = _approved(root, invite, request)
    root_seed = bytes.fromhex(root.private_hex)
    expected_id = fleet_enroll.assigned_machine_id(root_seed, request)

    key = machine_boot.complete_enrollment(
        delivery,
        request,
        root_seed,
        invite=invite,
        channel_binding=CHANNEL,
    )

    assert machine_boot.machine_id() == expected_id
    assert machine_boot.operating_key(root_seed).public_hex == key.public_hex
    assert key.public_hex == delivery.roster_entry.machine_pub
    # Assert the logical durable shape, not a byte substring inside SQLite
    # pages (SQLite is free to encode/compress/reuse page content).
    assert machine_boot._read_row(org="machine") == {"machine_id": expected_id}


def test_tampered_delivery_writes_no_identity(machine):
    root, invite = _invite()
    request, _ = machine_boot.first_boot(invite)
    delivery = _approved(root, invite, request)
    bad = replace(
        delivery,
        approval=replace(delivery.approval, enrollment_nonce="ff" * 32),
    )

    with pytest.raises(MachineBootError, match="different enrollment ceremony"):
        machine_boot.complete_enrollment(
            bad,
            request,
            bytes.fromhex(root.private_hex),
            invite=invite,
            channel_binding=CHANNEL,
        )
    assert machine_boot.has_identity() is False


def test_an_enrolled_machine_cannot_restart_or_replace_its_identity(machine):
    root, invite = _invite()
    request, _ = machine_boot.first_boot(invite)
    delivery = _approved(root, invite, request)
    machine_boot.complete_enrollment(
        delivery,
        request,
        bytes.fromhex(root.private_hex),
        invite=invite,
        channel_binding=CHANNEL,
    )

    with pytest.raises(MachineBootError, match="already enrolled"):
        machine_boot.first_boot(invite)
    with pytest.raises(MachineBootError, match="already enrolled"):
        machine_boot.complete_enrollment(
            delivery,
            request,
            bytes.fromhex(root.private_hex),
            invite=invite,
            channel_binding=CHANNEL,
        )


def test_operating_key_before_completed_enrollment_is_refused(machine):
    with pytest.raises(MachineBootError, match="not enrolled"):
        machine_boot.operating_key(bytes(range(32)))
