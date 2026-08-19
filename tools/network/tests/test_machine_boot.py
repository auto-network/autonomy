"""First boot mints the machine id and asks to join (auto-b6fee, thin flow).

The machine mints its own id, stores ONLY the id (public) in machine.db, and
its operating key DERIVES from personal_root + machine_id on demand — never
generated, never stored, never sealed. Idempotent second boot; no-orphan on
decline; the derived key matches what the primary derives from the same id.
"""

from __future__ import annotations

import pytest

from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair, derive_machine_key
from tools.network import fleet_invite, fleet_enroll, machine_boot
from tools.network.machine_boot import MachineBootError


@pytest.fixture
def machine(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db("machine", type_="personal", path=orgs.parent / "machine.db").close()
    yield tmp_path
    GraphDB.close_all_pooled()


def _invite():
    root = KeyPair.generate()
    return root, fleet_invite.mint(
        root, rendezvous="https://primary.example.net/rv/x", invite_id="ab" * 32,
    )


def test_first_boot_mints_an_id_and_stores_only_the_id_in_machine_db(machine):
    _, invite = _invite()
    req, fp = machine_boot.first_boot(invite)
    assert len(req.machine_id) == 64
    assert req.invite_id == invite.invite_id
    assert req.personal_root_pub == invite.personal_root_pub
    assert fp == fleet_enroll.fingerprint(req.machine_id)

    # The id is in machine.db, and there is NO key material anywhere: the row
    # holds only the public id, and personal.db is untouched.
    machine_db = (machine / "machine.db").read_bytes()
    personal = (machine / "personal.db").read_bytes()
    assert req.machine_id.encode() in machine_db
    assert req.machine_id.encode() not in personal
    # No vault store was ever created — the key is derived, not sealed.
    assert not (machine / "machine-vault.db").exists()


def test_the_operating_key_derives_from_root_and_id_and_matches_the_primary(machine):
    """The machine's key is DERIVED, not stored: given the provisioned root it
    re-derives, and the PRIMARY derives the identical public half from the same
    presented id — which is what it writes to the roster."""
    root_kp, invite = _invite()
    req, _ = machine_boot.first_boot(invite)
    root_seed = bytes.fromhex(root_kp.private_hex)

    machine_side = machine_boot.operating_key(root_seed)
    primary_side = derive_machine_key(root_seed, req.machine_id)
    assert machine_side.public_hex == primary_side.public_hex
    # Re-derivation is stable (nothing stored between calls).
    assert machine_boot.operating_key(root_seed).public_hex == machine_side.public_hex


def test_second_boot_reuses_the_same_id(machine):
    _, invite = _invite()
    req1, _ = machine_boot.first_boot(invite)
    assert machine_boot.has_identity()
    with pytest.raises(MachineBootError, match="already has an id"):
        machine_boot.first_boot(invite)
    assert machine_boot.machine_id() == req1.machine_id


def test_declining_leaves_no_orphan_and_a_fresh_boot_mints_a_new_id(machine):
    _, invite = _invite()
    req1, _ = machine_boot.first_boot(invite)
    assert machine_boot.discard() is True
    assert machine_boot.has_identity() is False
    req2, _ = machine_boot.first_boot(invite)
    assert req2.machine_id != req1.machine_id


def test_the_join_request_carries_no_key_and_no_secret(machine):
    """The join is the id over the authenticated tunnel — no key PoP (there is
    no key yet), no signature. Every field is public."""
    _, invite = _invite()
    req, _ = machine_boot.first_boot(invite)
    for value in (req.machine_id, req.invite_id, req.personal_root_pub):
        assert isinstance(value, str) and len(value) in (64,)
    assert not hasattr(req, "proof") and not hasattr(req, "signature")


def test_operating_key_before_first_boot_is_refused(machine):
    with pytest.raises(MachineBootError, match="no id"):
        machine_boot.operating_key(bytes(range(32)))
