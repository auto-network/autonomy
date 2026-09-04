"""A browser-minted fleet KICK tombstone verifies in Python.

The browser (fleet-kick.js) and Python (tools.network.fleet_roster.kick) must
agree byte-for-byte on the roster-entry signing input; otherwise a kick signed
in the operator's browser would not verify against the stored anchor. This
drives the real JS through node and checks the result with fleet_roster.verify —
the same guard the /api/fleet/machines/kick route applies.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network import fleet_roster
from tools.network.idkit import KeyPair


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DRIVER = (
    REPO_ROOT
    / "tools/dashboard/static/js/ceremony/tests/fleet-kick-vector.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_minted_kick_verifies_in_python(tmp_path):
    root = KeyPair.from_private_hex(bytes(range(32)).hex())
    fixture = {
        "personal_root_seed": root.private_hex,
        "root_pub": root.public_hex,
        "machine_id": "ab" * 32,
        "machine_public_key": "cd" * 32,
        "seq": 4,
        "issued_at": 1_900_000_000_000,
    }
    fixture_path = tmp_path / "fleet-kick.json"
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

    entry = fleet_roster.RosterEntry.from_dict(minted["roster_entry"])
    # The browser mints seq+1 so the tombstone beats the entry it revokes.
    assert entry.seq == fixture["seq"] + 1
    assert entry.kind is fleet_roster.EntryKind.KICK
    assert entry.supersedes is None
    assert entry.machine_id == fixture["machine_id"]
    assert entry.machine_pub == fixture["machine_public_key"]
    # The Python entry_id (content hash of the signed body) matches the browser's.
    assert entry.entry_id == minted["entry_id"]

    # The whole point: it verifies against the fleet anchor, exactly as the
    # route will check it.
    fleet_roster.verify(entry, anchor_root_pub=root.public_hex)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_kick_does_not_verify_under_a_foreign_anchor(tmp_path):
    root = KeyPair.from_private_hex(bytes(range(32)).hex())
    fixture = {
        "personal_root_seed": root.private_hex,
        "root_pub": root.public_hex,
        "machine_id": "ab" * 32,
        "machine_public_key": "cd" * 32,
        "seq": 0,
        "issued_at": 0,
    }
    fixture_path = tmp_path / "fleet-kick-foreign.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    result = subprocess.run(
        ["node", str(NODE_DRIVER), str(fixture_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    entry = fleet_roster.RosterEntry.from_dict(json.loads(result.stdout)["roster_entry"])
    with pytest.raises(fleet_roster.FleetRosterError):
        fleet_roster.verify(entry, anchor_root_pub="ee" * 32)
