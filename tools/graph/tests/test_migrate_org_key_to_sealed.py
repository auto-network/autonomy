"""Retrofitting a founded org from a passphrase-armored root key to a sealed one.

A revision-1 org root is encrypted under that ORGANIZATION's passphrase, so a
personal unlock cannot open it. Every capability reached from a personal
sign-on is therefore unavailable to such an org -- serving-certificate renewal
above all, which is why those certificates expire unattended.

It is a RE-WRAP, like re-sealing: same org root, new wrapping. The ledger's
commitment to the root is never touched, which is what lets it be permitted at
all after the founding lock.
"""

from __future__ import annotations

import pytest

from tools.graph import org_ops, settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_ORG_KEY_REVISION,
    NETWORK_ORG_KEY_SET_ID,
    ORG_ROOT_ARMOR_PURPOSE,
)
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import decrypt_root_key, encrypt_root_key
from tools.network.idkit.errors import SignatureError
from tools.network.idkit.sealing import derive_encapsulation_keypair, seal
from tools.network.idkit.sealing import open as seal_open

OWNER = bytes(range(32))
ORG_PASSPHRASE = "the organization's own passphrase"


def sealed_to(owner_seed: bytes, org_root: KeyPair) -> dict:
    _, recipient = derive_encapsulation_keypair(
        owner_seed, ORG_ROOT_ARMOR_PURPOSE)
    record = seal(
        bytes.fromhex(org_root.private_hex), recipient, ORG_ROOT_ARMOR_PURPOSE
    )
    return {
        "root_pub": org_root.public_hex,
        "sealed_root_key": record.hex(),
        "owner_kem_pub": recipient,
        "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
    }


def signed(slug, payload, key):
    return key.sign_hex(org_ops.reseal_input(slug, payload))


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """A FOUNDED org whose root key is passphrase-armored — revision 1, the
    shape a personal unlock cannot open."""
    from tools.graph.db import GraphDB
    from tools.network.ledger.found import found_org_ledger
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path

    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs / "personal.db").close()

    ref = org_ops.create_org_shell("acme")
    org_root = KeyPair.generate()
    settings_ops.add_setting(
        NETWORK_ORG_KEY_SET_ID,
        NETWORK_ORG_KEY_REVISION,
        "default",
        {
            "armored_private_key": encrypt_root_key(
                org_root, ORG_PASSPHRASE, iterations=10_000),
            "root_pub": org_root.public_hex,
        },
        org="acme",
    )
    with LedgerStore(org_ledger_db_path("acme")) as store:
        found_org_ledger(
            store, org_id=ref.id, org_root=org_root,
            personal_root_seed=OWNER, now=1_800_000_000_000,
        )
    yield org_root
    GraphDB.close_all_pooled()


# ── the problem, demonstrated before the fix ──


def test_a_personal_unlock_cannot_open_an_armored_org_root(legacy):
    """The whole reason for the migration: the personal seed opens nothing
    here, because this key answers to the organization's passphrase."""
    assert org_ops._current_sealed_org_key("acme") is None
    row = org_ops._current_legacy_org_key("acme")
    assert row is not None and row[1]["armored_private_key"]


def test_storing_is_refused_once_founded(legacy):
    """The founding lock holds, which is why the retrofit needs its own path."""
    with pytest.raises(org_ops.OrgExistsError, match="locked"):
        org_ops.store_sealed_org_key("acme", sealed_to(OWNER, legacy))


def test_resealing_is_refused_with_nothing_sealed_yet(legacy):
    """And re-seal assumes a sealed row already exists — it is the ROTATION
    path, not the retrofit."""
    payload = sealed_to(OWNER, legacy)
    with pytest.raises(org_ops.OrgError, match="no sealed root key"):
        org_ops.reseal_org_key("acme", payload, signed("acme", payload, legacy))


# ── the migration ──


def test_the_armored_root_migrates_to_a_sealed_one(legacy):
    payload = sealed_to(OWNER, legacy)
    org_ops.migrate_org_key_to_sealed(
        "acme", payload, signed("acme", payload, legacy))

    # The sealed row is now the org's key, and the armored row is GONE — not
    # left alongside, where resolution would keep returning revision 1.
    stored = org_ops._current_sealed_org_key("acme")
    assert stored is not None
    assert org_ops._current_legacy_org_key("acme") is None

    # The personal seed now opens the org root, and it is the SAME root the
    # ledger committed to — a re-wrap, not a replacement.
    owner_priv, _ = derive_encapsulation_keypair(OWNER, ORG_ROOT_ARMOR_PURPOSE)
    recovered = seal_open(
        bytes.fromhex(stored["sealed_root_key"]), owner_priv,
        ORG_ROOT_ARMOR_PURPOSE,
    )
    assert KeyPair.from_private_hex(recovered.hex()).public_hex == \
        legacy.public_hex
    assert stored["root_pub"] == legacy.public_hex


def test_the_org_passphrase_still_opens_nothing_it_should_not(legacy):
    """The armored blob is destroyed with the row. What the passphrase opened
    before the migration is exactly what the personal seed opens after — the
    same org root, no second copy left behind under the old lock."""
    before = decrypt_root_key(
        org_ops._current_legacy_org_key("acme")[1]["armored_private_key"],
        ORG_PASSPHRASE,
    )
    payload = sealed_to(OWNER, legacy)
    org_ops.migrate_org_key_to_sealed(
        "acme", payload, signed("acme", payload, legacy))

    owner_priv, _ = derive_encapsulation_keypair(OWNER, ORG_ROOT_ARMOR_PURPOSE)
    after = seal_open(
        bytes.fromhex(org_ops._current_sealed_org_key("acme")["sealed_root_key"]),
        owner_priv, ORG_ROOT_ARMOR_PURPOSE,
    )
    assert bytes(after).hex() == before.private_hex


def test_migrating_twice_is_a_no_op(legacy):
    payload = sealed_to(OWNER, legacy)
    sig = signed("acme", payload, legacy)
    org_ops.migrate_org_key_to_sealed("acme", payload, sig)
    first = org_ops._current_sealed_org_key("acme")
    org_ops.migrate_org_key_to_sealed("acme", payload, sig)
    assert org_ops._current_sealed_org_key("acme") == first


# ── refusals: the armored row must survive every one of them ──


def test_a_payload_naming_a_different_root_is_refused(legacy):
    impostor = KeyPair.generate()
    payload = sealed_to(OWNER, impostor)
    with pytest.raises(org_ops.OrgError, match="may not change the org root"):
        org_ops.migrate_org_key_to_sealed(
            "acme", payload, signed("acme", payload, impostor))
    assert org_ops._current_legacy_org_key("acme") is not None
    assert org_ops._current_sealed_org_key("acme") is None


def test_a_signature_from_anyone_but_the_org_root_is_refused(legacy):
    """Only a caller who can ALREADY open the org root may change what wraps
    it. Without this the server could be handed a blob sealed to an attacker
    and would lock the owner out permanently."""
    stranger = KeyPair.generate()
    payload = sealed_to(OWNER, legacy)
    with pytest.raises(SignatureError):
        org_ops.migrate_org_key_to_sealed(
            "acme", payload, signed("acme", payload, stranger))
    assert org_ops._current_legacy_org_key("acme") is not None
    assert org_ops._current_sealed_org_key("acme") is None


def test_an_authorization_for_one_migration_authorizes_no_other(legacy):
    """The signature binds the exact ciphertext and recipient, so a captured
    one cannot be replayed onto a different seal."""
    payload = sealed_to(OWNER, legacy)
    sig = signed("acme", payload, legacy)
    other = sealed_to(bytes(reversed(range(32))), legacy)
    with pytest.raises(SignatureError):
        org_ops.migrate_org_key_to_sealed("acme", other, sig)
    assert org_ops._current_legacy_org_key("acme") is not None


def test_the_sealed_row_takes_over_the_instant_it_is_written(legacy):
    """WHY VERIFICATION CANNOT BE LEFT TO WRITE ORDERING.

    The moment the sealed row exists it is what every revision-agnostic reader
    serves -- including the browser's own org-key route -- even though the
    armored row is still present. So writing the seal first does NOT hold the
    organization on its old key while the new one is checked: a seal nobody
    could open would take effect immediately, and deleting the armored row
    afterwards would only remove the fallback that was never being used.

    The server cannot close this itself; it has no personal seed and so cannot
    open the seal it is being handed. The check therefore belongs to the
    caller, which holds both the org root and the personal seed at that moment:
    seal, re-open, compare against root_pub, and only then submit.
    """
    from tools.graph.settings_ops import read_owned_set

    payload = sealed_to(OWNER, legacy)
    settings_ops.upsert_by_key(
        NETWORK_ORG_KEY_SET_ID, 2, "default",
        {
            "root_pub": payload["root_pub"],
            "sealed_root_key": payload["sealed_root_key"],
            "owner_kem_pub": payload["owner_kem_pub"],
            "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
        },
        org="acme",
    )

    served = [m.payload for m in read_owned_set(NETWORK_ORG_KEY_SET_ID, org="acme")]
    assert len(served) == 1
    assert served[0].get("sealed_root_key"), \
        "the sealed row wins immediately — ordering buys no verification window"
    assert org_ops._current_legacy_org_key("acme") is not None, \
        "while the armored row still exists, unread"

    # Completing the migration removes the row nobody was reading anyway.
    org_ops.migrate_org_key_to_sealed(
        "acme", payload, signed("acme", payload, legacy))
    served = [m.payload for m in read_owned_set(NETWORK_ORG_KEY_SET_ID, org="acme")]
    assert len(served) == 1
    assert served[0].get("sealed_root_key")
    assert not served[0].get("armored_private_key")
