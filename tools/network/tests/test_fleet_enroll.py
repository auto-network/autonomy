"""The one-time fleet enrollment ceremony and authorization boundary."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tools.network import fleet_enroll, fleet_invite, fleet_roster
from tools.network.fleet_enroll import FleetEnrollError
from tools.network.idkit import KeyPair, derive_machine_key


CHANNEL = "ca" * 32


def _invite(root=None, *, invite_id="ab" * 32):
    root = root or KeyPair.generate()
    return root, fleet_invite.mint(
        root,
        rendezvous="https://primary.example.net/fleet/rv/x",
        invite_id=invite_id,
    )


def _signed_evidence(root, invite, request, *, channel=CHANNEL, issued_at=123):
    seed = bytes.fromhex(root.private_hex)
    machine_id = fleet_enroll.assigned_machine_id(request)
    machine_key = derive_machine_key(seed, machine_id)
    entry = fleet_roster.enroll(
        root,
        machine_id=machine_id,
        machine_pub=machine_key.public_hex,
        assignment=fleet_roster.FLEET_MEMBER_ASSIGNMENT,
        issued_at=issued_at,
    )
    draft = fleet_enroll.approval_draft(
        request,
        invite=invite,
        channel_binding=channel,
        roster_entry=entry,
    )
    approval = replace(
        draft, signature=root.sign_hex(draft.signing_input())
    )
    return approval, entry


def _delivery(root, invite, request, *, channel=CHANNEL, org=None):
    approval, entry = _signed_evidence(
        root, invite, request, channel=channel
    )
    return fleet_enroll.authorize_request(
        approval,
        request,
        invite=invite,
        channel_binding=channel,
        roster_entry=entry,
        anchor_root_pub=root.public_hex,
        org=org,
    )


def test_request_carries_public_machine_identity_but_no_key():
    _, invite = _invite()
    request = fleet_enroll.build_request(
        invite=invite, machine_id="11" * 32
    )

    assert request.machine_id == "11" * 32
    assert request.invite_id == invite.invite_id
    assert request.personal_root_pub == invite.personal_root_pub
    for forbidden in ("machine_pub", "proof", "signature"):
        assert not hasattr(request, forbidden)


def test_both_dashboards_render_the_same_complete_request_code():
    _, invite = _invite()
    request = fleet_enroll.build_request(
        invite=invite, machine_id="22" * 32
    )
    joining_dashboard = fleet_enroll.verification_code(request)
    original_dashboard = fleet_enroll.verification_code(request)

    assert joining_dashboard == original_dashboard
    groups = joining_dashboard.split(" ")
    assert len(groups) == 6
    assert all(len(group) == 4 and group == group.upper() for group in groups)
    changed = replace(request, machine_id="23" * 32)
    assert fleet_enroll.verification_code(changed) != joining_dashboard


def test_request_is_bound_to_invite_and_fleet_anchor():
    root, invite = _invite()
    request = fleet_enroll.build_request(invite=invite)
    fleet_enroll.verify_request(request, invite=invite)

    _, another_invite = _invite(root, invite_id="cd" * 32)
    with pytest.raises(FleetEnrollError, match="different invite"):
        fleet_enroll.verify_request(request, invite=another_invite)

    foreign_root, _ = _invite()
    wrong_anchor = replace(
        another_invite,
        invite_id=invite.invite_id,
        personal_root_pub=foreign_root.public_hex,
    )
    with pytest.raises(FleetEnrollError, match="different fleet anchor"):
        fleet_enroll.verify_request(request, invite=wrong_anchor)


def test_browser_evidence_binds_durable_authority_and_transient_channel():
    root, invite = _invite()
    request = fleet_enroll.build_request(
        invite=invite, machine_id="33" * 32
    )
    approval, entry = _signed_evidence(root, invite, request)

    machine_id = fleet_enroll.assigned_machine_id(request)
    assert machine_id == request.machine_id
    assert entry.machine_id == machine_id
    assert entry.assignment == fleet_roster.FLEET_MEMBER_ASSIGNMENT
    assert approval.roster_entry_id == entry.entry_id
    assert approval.channel_binding == CHANNEL
    fleet_enroll.verify_approval(
        approval,
        request,
        invite=invite,
        channel_binding=CHANNEL,
        roster_entry=entry,
        anchor_root_pub=root.public_hex,
    )


def test_evidence_is_deterministic_for_safe_post_commit_retry():
    root, invite = _invite()
    request = fleet_enroll.build_request(
        invite=invite, machine_id="44" * 32
    )
    first_approval, first_entry = _signed_evidence(root, invite, request)
    retry_approval, retry_entry = _signed_evidence(root, invite, request)

    assert retry_entry.entry_id == first_entry.entry_id
    assert retry_entry.machine_id == first_entry.machine_id
    assert retry_entry.machine_pub == first_entry.machine_pub
    assert retry_approval.to_dict() == first_approval.to_dict()


def test_authorization_commits_roster_before_returning_delivery(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    root, invite = _invite()
    request = fleet_enroll.build_request(invite=invite)

    delivery = _delivery(root, invite, request, org="personal")
    stored = fleet_roster.current_roster(root.public_hex, org="personal")
    assert delivery.roster_entry.machine_pub in stored
    assert (
        stored[delivery.roster_entry.machine_pub].entry_id
        == delivery.roster_entry.entry_id
    )
    GraphDB.close_all_pooled()


def test_storage_failure_yields_no_deliverable_approval(monkeypatch):
    root, invite = _invite()
    request = fleet_enroll.build_request(invite=invite)
    approval, entry = _signed_evidence(root, invite, request)

    def fail_store(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(fleet_roster, "store_entry", fail_store)
    with pytest.raises(OSError, match="disk unavailable"):
        fleet_enroll.authorize_request(
            approval,
            request,
            invite=invite,
            channel_binding=CHANNEL,
            roster_entry=entry,
            anchor_root_pub=root.public_hex,
        )


def test_distinct_ceremonies_receive_distinct_machine_assignments():
    root, invite = _invite()
    first = fleet_enroll.build_request(
        invite=invite, machine_id="55" * 32
    )
    second = fleet_enroll.build_request(
        invite=invite, machine_id="56" * 32
    )

    assert (
        fleet_enroll.assigned_machine_id(first)
        != fleet_enroll.assigned_machine_id(second)
    )


def test_joiner_verifies_assignment_and_derives_authorized_key():
    root, invite = _invite()
    request = fleet_enroll.build_request(invite=invite)
    delivery = _delivery(root, invite, request)
    root_seed = bytes.fromhex(root.private_hex)
    origin_id = "01" * 32
    origin_key = derive_machine_key(root_seed, origin_id)
    origin_entry = fleet_roster.enroll(
        root,
        machine_id=origin_id,
        machine_pub=origin_key.public_hex,
        assignment=fleet_roster.FLEET_MEMBER_ASSIGNMENT,
        issued_at=122,
    )
    delivery = replace(
        delivery,
        origin_entry=origin_entry,
    )

    machine_id, key = fleet_enroll.verify_delivery(
        delivery,
        request,
        invite=invite,
        channel_binding=CHANNEL,
        personal_root_seed=root_seed,
    )
    assert machine_id == fleet_enroll.assigned_machine_id(request)
    assert key.public_hex == delivery.roster_entry.machine_pub


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("machine_id", "66" * 32, "different enrollment ceremony"),
        ("invite_id", "67" * 32, "different invite"),
        ("channel_binding", "68" * 32, "different invitation channel"),
        ("roster_entry_id", "69" * 32, "different roster entry"),
    ],
)
def test_tampered_transient_approval_is_refused(field, value, message):
    root, invite = _invite()
    request = fleet_enroll.build_request(invite=invite)
    approval, entry = _signed_evidence(root, invite, request)

    with pytest.raises(FleetEnrollError, match=message):
        fleet_enroll.verify_approval(
            replace(approval, **{field: value}),
            request,
            invite=invite,
            channel_binding=CHANNEL,
            roster_entry=entry,
            anchor_root_pub=root.public_hex,
        )


@pytest.mark.parametrize("field", ["machine_id", "machine_pub", "assignment"])
def test_tampered_durable_authority_is_refused(field):
    root, invite = _invite()
    request = fleet_enroll.build_request(invite=invite)
    approval, entry = _signed_evidence(root, invite, request)
    value = "ff" * 32 if field != "assignment" else "fleet_admin"

    with pytest.raises(FleetEnrollError):
        fleet_enroll.verify_approval(
            approval,
            request,
            invite=invite,
            channel_binding=CHANNEL,
            roster_entry=replace(entry, **{field: value}),
            anchor_root_pub=root.public_hex,
        )


def test_artifact_signer_cannot_replace_server_resolved_root():
    root, invite = _invite()
    request = fleet_enroll.build_request(invite=invite)
    approval, entry = _signed_evidence(root, invite, request)
    foreign = KeyPair.generate()

    with pytest.raises(FleetEnrollError, match="stored personal root"):
        fleet_enroll.verify_approval(
            approval,
            request,
            invite=invite,
            channel_binding=CHANNEL,
            roster_entry=entry,
            anchor_root_pub=foreign.public_hex,
        )
