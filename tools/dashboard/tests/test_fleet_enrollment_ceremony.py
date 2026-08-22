"""Browser-minted fleet evidence verifies under the Python implementation."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network import (
    fleet_enroll,
    fleet_invite,
    fleet_roster,
    fleet_runtime,
    machine_boot,
)
from tools.network.idkit import (
    KeyPair,
    Subject,
    issue_cert,
    verify_chain,
)
from tools.network.idkit.keys import verify_signature


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DRIVER = (
    REPO_ROOT / "tools/dashboard/static/js/ceremony/tests/fleet-enrollment-vector.mjs"
)
COMPLETION_DRIVER = (
    REPO_ROOT
    / "tools/dashboard/static/js/ceremony/tests/fleet-enrollment-completion-vector.mjs"
)
CERT_VECTOR_DRIVER = (
    REPO_ROOT
    / "tools/dashboard/static/js/lib/tests/verify-cert-vector.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_minted_roster_and_transient_approval_verify_in_python(tmp_path):
    root = KeyPair.from_private_hex(bytes(range(32)).hex())
    invite = fleet_invite.mint(
        root,
        rendezvous="https://primary.example.test/fleet/request",
        invite_id="ab" * 32,
    )
    request = fleet_enroll.build_request(
        invite=invite,
        machine_id="cd" * 32,
    )
    fixture = {
        "personal_root_seed": root.private_hex,
        "root_pub": root.public_hex,
        "request": {
            "machine_id": request.machine_id,
            "personal_root_pub": request.personal_root_pub,
            "invite_id": request.invite_id,
        },
        "channel_binding": "ef" * 32,
        "issued_at": 1_777_000_123_456,
        "seq": 0,
        "local_bootstrap_machine_id": "12" * 32,
    }
    fixture_path = tmp_path / "fleet-enrollment.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    result = subprocess.run(
        ["node", str(NODE_DRIVER), str(fixture_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    minted = json.loads(result.stdout)
    assert minted["root_seed_zeroed"] is True

    roster_entry = fleet_roster.RosterEntry.from_dict(minted["rosterEntry"])
    approval = fleet_enroll.EnrollmentApproval.from_dict(minted["approval"])
    fleet_enroll.verify_approval(
        approval,
        request,
        invite=invite,
        channel_binding=fixture["channel_binding"],
        roster_entry=roster_entry,
        anchor_root_pub=root.public_hex,
    )
    assert roster_entry.machine_id == fleet_enroll.assigned_machine_id(request)
    assert roster_entry.assignment == fleet_roster.FLEET_MEMBER_ASSIGNMENT
    assert approval.roster_entry_id == roster_entry.entry_id
    local_entry = fleet_roster.RosterEntry.from_dict(minted["localRosterEntry"])
    fleet_roster.verify(local_entry, anchor_root_pub=root.public_hex)
    assert local_entry.machine_id == fixture["local_bootstrap_machine_id"]
    assert local_entry.machine_id != roster_entry.machine_id
    assert local_entry.assignment == fleet_roster.FLEET_MEMBER_ASSIGNMENT
    local_runtime = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        minted["localRuntime"],
        personal_root_pub=root.public_hex,
        roster_entries=[local_entry, roster_entry],
    )
    assert local_runtime.machine_id == local_entry.machine_id
    assert local_runtime.machine_pub == local_entry.machine_pub


def test_fleet_domains_are_distinct_and_versioned():
    assert fleet_roster.FLEET_ROSTER_DOMAIN == b"autonomy.fleet.roster-entry.v1\n"
    assert fleet_enroll.FLEET_APPROVAL_DOMAIN == b"autonomy.fleet.enrollment-approval.v1\n"
    assert fleet_roster.FLEET_ROSTER_DOMAIN != fleet_enroll.FLEET_APPROVAL_DOMAIN


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_machine_runtime_certificate_verifies_in_python_and_browser(tmp_path):
    machine = KeyPair.from_private_hex("45" * 32)
    process = KeyPair.from_private_hex("67" * 32)
    root_pub = "89" * 32
    org = f"personal:{root_pub}"
    cert = issue_cert(
        machine,
        process.public_hex,
        scope=["fleet:sync"],
        org=org,
        subject=Subject(kind="machine", id="ab" * 32),
        not_before=1_777_000_000,
        not_after=1_777_000_300,
    )
    verified = verify_chain(
        cert,
        machine.public_hex,
        org=org,
        now=1_777_000_100,
        required_scope="fleet:sync",
    )
    assert verified.subject_kind == "machine"
    fixture = {
        "cert_wire": cert.to_json().decode(),
        "root_pub": machine.public_hex,
        "org": org,
        "now": 1_777_000_100,
    }
    path = tmp_path / "machine-cert.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    result = subprocess.run(
        ["node", str(CERT_VECTOR_DRIVER), str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    browser = json.loads(result.stdout)
    assert browser == {
        "child_pub": process.public_hex,
        "scope": ["fleet:sync"],
        "subject": {"kind": "machine", "id": "ab" * 32},
        "org": org,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_completion_verifies_delivery_and_proves_machine_key(tmp_path):
    root = KeyPair.from_private_hex(bytes(range(32)).hex())
    invite = fleet_invite.mint(
        root,
        rendezvous="https://primary.example.test/fleet/request",
        invite_id="ab" * 32,
    )
    request = fleet_enroll.build_request(
        invite=invite, machine_id="cd" * 32
    )
    machine_id = fleet_enroll.assigned_machine_id(request)
    machine_key = fleet_enroll.derive_machine_key(
        bytes.fromhex(root.private_hex), machine_id
    )
    roster = fleet_roster.enroll(
        root, machine_id=machine_id, machine_pub=machine_key.public_hex,
        issued_at=1_777_000_123_456,
    )
    channel = "ef" * 32
    draft = fleet_enroll.approval_draft(
        request,
        invite=invite,
        channel_binding=channel,
        roster_entry=roster,
    )
    approval = fleet_enroll.EnrollmentApproval(
        **{**draft.__dict__, "signature": root.sign_hex(draft.signing_input())}
    )
    request_id = fleet_enroll.request_id(request)
    fixture = {
        "personal_root_seed": root.private_hex,
        "request_id": request_id,
        "request": request.to_dict(),
        "channel_binding": channel,
        "approval": approval.to_dict(),
        "roster_entry": roster.to_dict(),
    }
    path = tmp_path / "fleet-completion.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    result = subprocess.run(
        ["node", str(COMPLETION_DRIVER), str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    completion = json.loads(result.stdout)
    assert completion["root_seed_zeroed"] is True
    assert completion["machine_id"] == machine_id
    verify_signature(
        roster.machine_pub,
        completion["proof"],
        machine_boot.completion_input(
            request_id=request_id,
            roster_entry_id=roster.entry_id,
        ),
    )
    runtime = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        completion["runtime"],
        personal_root_pub=root.public_hex,
        roster_entries=[roster],
    )
    assert runtime.machine_id == machine_id
    assert runtime.machine_pub == machine_key.public_hex
    assert runtime.process_key.public_hex == runtime.delegation_cert.child_pub
    assert runtime.process_key.public_hex != machine_key.public_hex
