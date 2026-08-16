"""Persona KEM credential publication end-to-end (auto-uh2dp).

The wiring this bead adds is exercised elsewhere at its own layer — the
founding/admission claim carrying a credential lives in the ledger and graph
test suites; the KeyControlStore round-trip and refusal live in
``test_keycontrol``; ``build``'s determinism and fold currency live in
``test_credentials``. This module pins the two acceptance criteria that cross
those layers: the root-derived seed (``derive_kem_seed``) and the full path a
grant travels — sealed to a credential RETRIEVED FROM THE STORE by
``kem_key_id`` and opened by its recipient.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.storagekit import capability, credentials
from tools.network.storagekit.credentials import derive_kem_seed
from tools.network.storagekit.errors import MalformedRecordError
from tools.network.storagekit.keycontrol import KeyControlStore

from .conftest import HLC0

GENESIS = "a" * 64


# -- derive_kem_seed: root-derived, secretless, rotation-ready ----------------------


def test_derive_kem_seed_is_deterministic_and_counter_varied():
    root = bytes(range(32))
    # Deterministic: a second call — standing in for a second machine running
    # the same pure derivation — yields the identical seed, no device entropy.
    assert derive_kem_seed(root) == derive_kem_seed(root)
    assert derive_kem_seed(root, 0) == derive_kem_seed(root)  # default is counter 0
    # Rotation varies the seed via the counter, never the fixed purpose label.
    assert derive_kem_seed(root, 0) != derive_kem_seed(root, 1)
    assert derive_kem_seed(root, 1) != derive_kem_seed(root, 2)
    # A different root is a different persona seed.
    assert derive_kem_seed(root) != derive_kem_seed(bytes(range(1, 33)))


def test_derive_kem_seed_rejects_bad_input():
    with pytest.raises(MalformedRecordError):
        derive_kem_seed("not-bytes")  # type: ignore[arg-type]
    with pytest.raises(MalformedRecordError):
        derive_kem_seed(bytes(32), -1)
    with pytest.raises(MalformedRecordError):
        derive_kem_seed(bytes(32), True)  # bool is not an int counter


def test_second_machine_derives_a_byte_identical_keypair():
    """Same root seed ⇒ byte-identical KEM keypair on a second machine, with
    no device-local randomness; the full record differs only where a signed,
    hashed field (``created_hlc``) differs (acceptance)."""
    root = bytes(range(7, 39))
    signing = KeyPair.from_private_hex((b"\x11" * 32).hex())

    seed_a = derive_kem_seed(root)
    cred_a, priv_a = credentials.build(signing, GENESIS, seed_a, [GENESIS], HLC0)

    # "Second machine": a fresh derivation from the same root, nothing shared.
    seed_b = derive_kem_seed(root)
    cred_b, priv_b = credentials.build(signing, GENESIS, seed_b, [GENESIS], HLC0)

    assert seed_a == seed_b
    assert priv_a == priv_b  # the KEM PRIVATE key is byte-identical
    assert cred_a.kem_public_key == cred_b.kem_public_key
    # Identical inputs ⇒ identical content-addressed record: no fork (§14).
    assert cred_a == cred_b

    # The keypair does not depend on the advisory created_hlc, but the record
    # does: same seed, later hlc ⇒ same keypair, different kem_key_id.
    cred_c, priv_c = credentials.build(
        signing, GENESIS, seed_a, [GENESIS], (HLC0[0] + 1, 0)
    )
    assert priv_c == priv_a
    assert cred_c.kem_public_key == cred_a.kem_public_key
    assert cred_c.kem_key_id != cred_a.kem_key_id


# -- grant seals to a store-retrieved credential ------------------------------------


def test_grant_seals_to_a_store_retrieved_credential_and_recipient_opens(world):
    """Publish a credential, retrieve it by kem_key_id, seal a grant to what
    the store returned, and open it — the full provisioning path."""
    creator, recipient = world.member(0), world.member(1)
    descriptor, secret = world.mint_initial_state(creator)
    entry = world.principals[recipient.public_hex]
    credential = entry["credential"]

    with KeyControlStore(":memory:") as store:
        store.accept_credential(credential)
        # Verified against the authority fold: a current content-holding member.
        assert credentials.verify_against_fold(credential, world.fold()) == credential
        retrieved = store.get_credential(credential.kem_key_id)
    assert retrieved == credential

    grant = capability.issue(
        creator,
        genesis_id=world.gen,
        domain_id=world.dom,
        storage_state_id=descriptor.state_id,
        recipient_credential=retrieved,  # the record the store handed back
        state_secret=secret,
        state_secret_commitment=descriptor.secret_commitment,
        authority_heads=world.frontier(),
    )
    assert capability.accept(grant, entry["kem_private"], descriptor) == secret


def test_store_retrieved_credential_is_unopenable_by_a_foreign_key(world):
    """Sealing to the retrieved credential binds the recipient: another
    member's private key cannot open the grant."""
    creator, recipient, stranger = world.member(0), world.member(1), world.member(2)
    descriptor, secret = world.mint_initial_state(creator)
    credential = world.principals[recipient.public_hex]["credential"]

    with KeyControlStore(":memory:") as store:
        store.accept_credential(credential)
        retrieved = store.get_credential(credential.kem_key_id)

    grant = capability.issue(
        creator,
        genesis_id=world.gen,
        domain_id=world.dom,
        storage_state_id=descriptor.state_id,
        recipient_credential=retrieved,
        state_secret=secret,
        state_secret_commitment=descriptor.secret_commitment,
        authority_heads=world.frontier(),
    )
    from tools.network.idkit.errors import SealingError

    stranger_private = world.principals[stranger.public_hex]["kem_private"]
    with pytest.raises(SealingError):
        capability.accept(grant, stranger_private, descriptor)
