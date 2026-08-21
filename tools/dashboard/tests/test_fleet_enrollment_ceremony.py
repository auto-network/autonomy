"""Browser-minted fleet evidence verifies under the Python implementation."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network import fleet_enroll, fleet_invite, fleet_roster
from tools.network.idkit import KeyPair


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DRIVER = (
    REPO_ROOT / "tools/dashboard/static/js/ceremony/tests/fleet-enrollment-vector.mjs"
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
        enrollment_nonce="cd" * 32,
    )
    fixture = {
        "personal_root_seed": root.private_hex,
        "root_pub": root.public_hex,
        "request": {
            "enrollment_nonce": request.enrollment_nonce,
            "personal_root_pub": request.personal_root_pub,
            "invite_id": request.invite_id,
        },
        "channel_binding": "ef" * 32,
        "issued_at": 1_777_000_123_456,
        "seq": 0,
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
    assert roster_entry.machine_id == fleet_enroll.assigned_machine_id(
        bytes.fromhex(root.private_hex), request
    )
    assert roster_entry.assignment == fleet_roster.FLEET_MEMBER_ASSIGNMENT
    assert approval.roster_entry_id == roster_entry.entry_id


def test_fleet_domains_are_distinct_and_versioned():
    assert fleet_roster.FLEET_ROSTER_DOMAIN == b"autonomy.fleet.roster-entry.v1\n"
    assert fleet_enroll.FLEET_APPROVAL_DOMAIN == b"autonomy.fleet.enrollment-approval.v1\n"
    assert fleet_roster.FLEET_ROSTER_DOMAIN != fleet_enroll.FLEET_APPROVAL_DOMAIN
