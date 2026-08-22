from __future__ import annotations

import sqlite3

import pytest

from tools.dashboard import fleet_enrollment_routes
from tools.graph.db import GraphDB
from tools.network import fleet_roster, fleet_runtime
from tools.network.idkit import KeyPair, Subject, issue_cert


NOW = 1_777_000_100


def _fixture():
    root = KeyPair.from_private_hex("12" * 32)
    machine = KeyPair.from_private_hex("34" * 32)
    process = KeyPair.from_private_hex("56" * 32)
    machine_id = "78" * 32
    entry = fleet_roster.enroll(
        root, machine_id=machine_id, machine_pub=machine.public_hex
    )
    cert = issue_cert(
        machine,
        process.public_hex,
        scope=["fleet:sync"],
        org=f"personal:{root.public_hex}",
        subject=Subject(kind="machine", id=machine_id),
        not_before=NOW - 30,
        not_after=NOW + 300,
    )
    payload = {
        "machine_id": machine_id,
        "machine_pub": machine.public_hex,
        "process_private_seed": process.private_hex,
        "delegation_cert": cert.to_dict(),
    }
    return root, machine, process, entry, payload


def test_browser_runtime_handoff_accepts_only_process_seed_and_public_proof():
    root, machine, process, entry, payload = _fixture()
    credential = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload,
        personal_root_pub=root.public_hex,
        roster_entries=[entry],
        now=NOW,
    )
    assert credential.machine_id == entry.machine_id
    assert credential.machine_pub == machine.public_hex
    assert credential.process_key.private_hex == process.private_hex
    assert "root" not in payload
    assert "machine_private" not in payload


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(process_private_seed="90" * 32),
    lambda p: p["delegation_cert"].update(scope=["fleet:sync", "settings:write"]),
    lambda p: p["delegation_cert"]["subject"].update(id="ab" * 32),
    lambda p: p["delegation_cert"].update(not_after=NOW - 1),
])
def test_browser_runtime_handoff_refuses_mismatch_escalation_and_expiry(mutation):
    root, _machine, _process, entry, payload = _fixture()
    payload = {
        **payload,
        "delegation_cert": {
            **payload["delegation_cert"],
            "subject": dict(payload["delegation_cert"]["subject"]),
            "scope": list(payload["delegation_cert"]["scope"]),
        },
    }
    mutation(payload)
    with pytest.raises(fleet_runtime.FleetRuntimeError):
        fleet_runtime.FleetRuntimeCredential.from_browser_payload(
            payload,
            personal_root_pub=root.public_hex,
            roster_entries=[entry],
            now=NOW,
        )


def test_runtime_activation_prepares_and_enables_personal_writer_capture(
    tmp_path, monkeypatch
):
    path = tmp_path / "personal.db"
    GraphDB(path).close()
    monkeypatch.setattr(
        fleet_enrollment_routes, "_org_db_path", lambda _org: path
    )
    machine_pub = "91" * 32

    fleet_enrollment_routes._ensure_fleet_catalog(machine_pub)

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT origin_incarnation FROM fleet_sync_state"
        ).fetchone()[0] == machine_pub
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'fleet_sync_%'"
        ).fetchone()[0] > 0
