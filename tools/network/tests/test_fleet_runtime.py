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


ORG_UUID = "6f1e2d3c-4b5a-4c6d-8e9f-0a1b2c3d4e5f"


def _reach_payload():
    root, machine, process, entry, payload = _fixture()
    rcert = issue_cert(
        root, machine.public_hex, scope=["node:announce", "node:lookup"],
        org=ORG_UUID, subject=Subject(kind="agent", id=payload["machine_id"]),
        not_before=NOW - 30, not_after=NOW + 3600)
    payload["machine_private_seed"] = machine.private_hex
    payload["reachability_cert"] = rcert.to_dict()
    return root, machine, entry, payload


def test_reachability_credential_accepted_when_org_registered():
    root, machine, entry, payload = _reach_payload()
    cred = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload, personal_root_pub=root.public_hex, roster_entries=[entry],
        org_uuid=ORG_UUID, now=NOW)
    assert cred.machine_key.public_hex == machine.public_hex
    assert set(cred.reachability_cert.scope) == {"node:announce", "node:lookup"}
    # the sync path is untouched: process key + fleet:sync cert still there
    assert cred.process_key.private_hex == payload["process_private_seed"]


def test_reachability_omitted_leaves_sync_only_credential():
    root, machine, process, entry, payload = _fixture()
    cred = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload, personal_root_pub=root.public_hex, roster_entries=[entry], now=NOW)
    assert cred.machine_key is None and cred.reachability_cert is None


def test_reachability_without_org_uuid_is_refused():
    root, machine, entry, payload = _reach_payload()
    with pytest.raises(fleet_runtime.FleetRuntimeError):
        fleet_runtime.FleetRuntimeCredential.from_browser_payload(
            payload, personal_root_pub=root.public_hex, roster_entries=[entry],
            org_uuid=None, now=NOW)


def test_reachability_cert_for_another_machine_is_refused():
    root, machine, entry, payload = _reach_payload()
    other = KeyPair.from_private_hex("99" * 32)
    bad = issue_cert(
        root, other.public_hex, scope=["node:announce", "node:lookup"],
        org=ORG_UUID, subject=Subject(kind="agent", id=payload["machine_id"]),
        not_before=NOW - 30, not_after=NOW + 3600)
    payload["reachability_cert"] = bad.to_dict()
    with pytest.raises(fleet_runtime.FleetRuntimeError):
        fleet_runtime.FleetRuntimeCredential.from_browser_payload(
            payload, personal_root_pub=root.public_hex, roster_entries=[entry],
            org_uuid=ORG_UUID, now=NOW)


def test_machine_seed_without_reachability_cert_is_refused():
    root, machine, process, entry, payload = _fixture()
    payload["machine_private_seed"] = machine.private_hex  # only one of the pair
    with pytest.raises(fleet_runtime.FleetRuntimeError):
        fleet_runtime.FleetRuntimeCredential.from_browser_payload(
            payload, personal_root_pub=root.public_hex, roster_entries=[entry],
            org_uuid=ORG_UUID, now=NOW)


def test_personal_org_uuid_is_deterministic_and_root_scoped():
    a = KeyPair.from_private_hex("12" * 32).public_hex
    b = KeyPair.from_private_hex("34" * 32).public_hex
    # Deterministic: the same personal root always yields the same org_uuid, so
    # any machine can register the personal org idempotently with no
    # coordination.
    assert fleet_runtime.personal_org_uuid(a) == fleet_runtime.personal_org_uuid(a)
    # A valid UUID string.
    import uuid as _uuid
    assert str(_uuid.UUID(fleet_runtime.personal_org_uuid(a))) == \
        fleet_runtime.personal_org_uuid(a)
    # Distinct roots get distinct org_uuids.
    assert fleet_runtime.personal_org_uuid(a) != fleet_runtime.personal_org_uuid(b)


def test_personal_org_uuid_rejects_non_pubkey():
    with pytest.raises(fleet_runtime.FleetRuntimeError):
        fleet_runtime.personal_org_uuid("not-a-pubkey")


# -- auto-e2ufw: optional per-org serving machine key in the payload --------

def test_serving_machine_seed_absent_leaves_fleet_key_as_hello_identity():
    root, _machine, _process, entry, payload = _fixture()
    cred = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload, personal_root_pub=root.public_hex,
        roster_entries=[entry], now=NOW,
    )
    # No serving seed -> no serving key -> the transitional fleet-key path.
    assert cred.serving_machine_key is None


def test_serving_machine_seed_present_is_parsed_as_a_distinct_key():
    from tools.network.idkit import derive_serving_machine_key
    root, _machine, _process, entry, payload = _fixture()
    genesis = "1a" * 32
    serving = derive_serving_machine_key(
        bytes.fromhex(root.private_hex), genesis, entry.machine_id)
    payload = {**payload,
               "serving_machine_private_seed": serving.private_hex}
    cred = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload, personal_root_pub=root.public_hex,
        roster_entries=[entry], now=NOW,
    )
    assert cred.serving_machine_key is not None
    assert cred.serving_machine_key.public_hex == serving.public_hex
    # Distinct from the fleet machine key: this is the whole point.
    assert cred.serving_machine_key.public_hex != cred.machine_pub


def test_malformed_serving_machine_seed_is_refused():
    root, _machine, _process, entry, payload = _fixture()
    payload = {**payload, "serving_machine_private_seed": "not-hex"}
    with pytest.raises(fleet_runtime.FleetRuntimeError):
        fleet_runtime.FleetRuntimeCredential.from_browser_payload(
            payload, personal_root_pub=root.public_hex,
            roster_entries=[entry], now=NOW,
        )


def test_serving_seed_composes_with_the_reachability_path():
    # The serving key is independent of the reachability machine_private_seed;
    # a payload may carry both, and the serving key is separate from the
    # fleet/reachability key.
    from tools.network.idkit import derive_serving_machine_key
    root, machine, _process, entry, payload = _fixture()
    genesis = "2b" * 32
    serving = derive_serving_machine_key(
        bytes.fromhex(root.private_hex), genesis, entry.machine_id)
    payload = {**payload,
               "serving_machine_private_seed": serving.private_hex}
    cred = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload, personal_root_pub=root.public_hex,
        roster_entries=[entry], now=NOW,
    )
    assert cred.serving_machine_key.public_hex == serving.public_hex
