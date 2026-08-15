"""Re-sealing a founded org's root key to a new owner key (auto-t1nek).

When an owner rotates their personal root, every seal made to the old one
stops opening for them -- including the root key of an organization they own.
They are locked out of something they still own, while the thief holding the
old personal root is not. Re-sealing is how that is repaired.

It is a RE-WRAP: same org root, new recipient. The ledger's commitment to the
root is never touched, which is what lets it be permitted at all after the
founding lock.
"""

from __future__ import annotations

import pytest

from tools.graph import org_ops
from tools.graph.schemas.network_identity import ORG_ROOT_ARMOR_PURPOSE
from tools.network.idkit import KeyPair
from tools.network.idkit.errors import SignatureError
from tools.network.idkit.sealing import derive_encapsulation_keypair, seal
from tools.network.idkit.sealing import open as seal_open

OLD_OWNER = bytes(range(32))
NEW_OWNER = bytes(reversed(range(32)))


def sealed_to(owner_seed: bytes, org_root: KeyPair) -> dict:
    _, recipient = derive_encapsulation_keypair(owner_seed, ORG_ROOT_ARMOR_PURPOSE)
    record = seal(
        bytes.fromhex(org_root.private_hex), recipient, ORG_ROOT_ARMOR_PURPOSE
    )
    return {
        "root_pub": org_root.public_hex,
        "sealed_root_key": record.hex(),
        "owner_kem_pub": recipient,
        "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
    }


@pytest.fixture
def founded(tmp_path, monkeypatch):
    """A founded org whose root is sealed to the owner's ORIGINAL personal root."""
    from tools.graph.db import GraphDB
    from tools.network.ledger.found import found_org_ledger
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path

    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs / "personal.db").close()

    ref = org_ops.create_org_shell("acme")
    org_root = KeyPair.generate()
    org_ops.store_sealed_org_key("acme", sealed_to(OLD_OWNER, org_root))
    with LedgerStore(org_ledger_db_path("acme")) as store:
        found_org_ledger(
            store, org_id=ref.id, org_root=org_root,
            personal_root_seed=OLD_OWNER, now=1_800_000_000_000,
        )
    yield org_root
    GraphDB.close_all_pooled()


def signed(slug, payload, key):
    return key.sign_hex(org_ops.reseal_input(slug, payload))


def test_the_owner_is_locked_out_after_rotating_until_the_key_is_resealed(founded):
    """The problem this exists to fix, demonstrated before the fix is applied."""
    stored = org_ops._current_sealed_org_key("acme")
    new_priv, _ = derive_encapsulation_keypair(NEW_OWNER, ORG_ROOT_ARMOR_PURPOSE)
    with pytest.raises(Exception):
        seal_open(
            bytes.fromhex(stored["sealed_root_key"]), new_priv, ORG_ROOT_ARMOR_PURPOSE
        )


def test_storing_is_refused_once_founded(founded):
    """The founding lock still holds: this is why re-seal needs its own path."""
    with pytest.raises(org_ops.OrgExistsError, match="locked"):
        org_ops.store_sealed_org_key("acme", sealed_to(NEW_OWNER, founded))


def test_the_org_root_can_be_resealed_to_the_new_owner_key(founded):
    payload = sealed_to(NEW_OWNER, founded)
    org_ops.reseal_org_key("acme", payload, signed("acme", payload, founded))

    stored = org_ops._current_sealed_org_key("acme")
    new_priv, _ = derive_encapsulation_keypair(NEW_OWNER, ORG_ROOT_ARMOR_PURPOSE)
    recovered = seal_open(
        bytes.fromhex(stored["sealed_root_key"]), new_priv, ORG_ROOT_ARMOR_PURPOSE
    )
    # Same organization root, reachable from the owner's NEW personal root.
    assert recovered.hex() == founded.private_hex
    assert stored["root_pub"] == founded.public_hex


def test_a_reseal_may_not_change_the_org_root(founded):
    """The ledger has committed to this root; a re-wrap is not a replacement."""
    impostor = KeyPair.generate()
    payload = sealed_to(NEW_OWNER, impostor)
    with pytest.raises(org_ops.OrgError, match="may not change the org root"):
        org_ops.reseal_org_key("acme", payload, signed("acme", payload, impostor))


def test_a_reseal_needs_the_org_root_signature(founded):
    """Otherwise anyone could overwrite the seal and lock the owner out."""
    payload = sealed_to(NEW_OWNER, founded)
    stranger = KeyPair.generate()
    with pytest.raises(SignatureError):
        org_ops.reseal_org_key("acme", payload, signed("acme", payload, stranger))


def test_an_authorisation_cannot_be_re_pointed_at_another_recipient(founded):
    """Approval to re-seal to one key must not re-seal to a different one."""
    intended = sealed_to(NEW_OWNER, founded)
    approval = signed("acme", intended, founded)
    attacker_seed = bytes([7] * 32)
    hijacked = sealed_to(attacker_seed, founded)
    with pytest.raises(SignatureError):
        org_ops.reseal_org_key("acme", hijacked, approval)


def test_an_authorisation_for_another_org_is_refused(founded):
    payload = sealed_to(NEW_OWNER, founded)
    elsewhere = signed("some-other-org", payload, founded)
    with pytest.raises(SignatureError):
        org_ops.reseal_org_key("acme", payload, elsewhere)


def test_resealing_an_org_with_no_stored_key_is_refused(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs / "personal.db").close()
    org_ops.create_org_shell("bare")
    root = KeyPair.generate()
    payload = sealed_to(NEW_OWNER, root)
    with pytest.raises(org_ops.OrgError, match="no sealed root key"):
        org_ops.reseal_org_key("bare", payload, signed("bare", payload, root))
    GraphDB.close_all_pooled()


def test_resealing_is_idempotent(founded):
    payload = sealed_to(NEW_OWNER, founded)
    sig = signed("acme", payload, founded)
    org_ops.reseal_org_key("acme", payload, sig)
    org_ops.reseal_org_key("acme", payload, sig)
    assert org_ops._current_sealed_org_key("acme")["owner_kem_pub"] == payload["owner_kem_pub"]
