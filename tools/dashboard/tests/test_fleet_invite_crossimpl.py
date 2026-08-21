"""A browser-minted fleet invitation verifies and decodes in Python."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network import fleet_invite
from tools.network.idkit import KeyPair


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DRIVER = (
    REPO_ROOT
    / "tools/dashboard/static/js/ceremony/tests/fleet-invite-vector.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_minted_invite_is_byte_stable_and_verifies_in_python(tmp_path):
    root = KeyPair.from_private_hex(bytes(range(32)).hex())
    fixture = {
        "personal_root_seed": root.private_hex,
        "root_pub": root.public_hex,
        "rendezvous": f"https://relay.auto.network/l/{'12' * 16}",
        "invite_id": "ab" * 32,
        "expires_at": 1_900_000_000_000,
    }
    fixture_path = tmp_path / "fleet-invite.json"
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

    invitation = fleet_invite.FleetInvite.from_dict(minted["invite"])
    fleet_invite.verify(invitation, expected_root_pub=root.public_hex)
    assert fleet_invite.decode(minted["code"]) == invitation
    assert fleet_invite.encode(invitation) == minted["code"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_invite_refuses_a_mismatched_root_and_zeroes_the_seed(tmp_path):
    root = KeyPair.from_private_hex("34" * 32)
    fixture = {
        "personal_root_seed": root.private_hex,
        "root_pub": "56" * 32,
        "rendezvous": f"https://relay.auto.network/l/{'12' * 16}",
        "invite_id": "ab" * 32,
        "expires_at": 0,
    }
    fixture_path = tmp_path / "fleet-invite-mismatch.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    script = (
        "import fs from 'node:fs';"
        "import {mintFleetInvite} from "
        f"'{(REPO_ROOT / 'tools/dashboard/static/js/ceremony/fleet-enrollment.js').as_uri()}';"
        "const f=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));"
        "const s=Uint8Array.from(Buffer.from(f.personal_root_seed,'hex'));"
        "let error='';try{await mintFleetInvite({personalRootSeed:s,"
        "rootPub:f.root_pub,rendezvous:f.rendezvous,inviteId:f.invite_id,"
        "expiresAt:f.expires_at});}catch(e){error=e.message;}"
        "process.stdout.write(JSON.stringify({error,zero:s.every(v=>v===0)}));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(fixture_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    refused = json.loads(result.stdout)
    assert refused == {
        "error": "opened personal root does not match rootPub",
        "zero": True,
    }
