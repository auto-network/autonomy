"""The sign-on handoff's key-opening core produces exactly the holder's cache.

Drives real grants (the same construction ``test_capability`` uses) through
``open_generation_keys`` and asserts the result is the ``{state_id: secret}``
mapping the vault key holder consumes — proving the sign-on wire and the read
path cannot disagree about the shape.
"""

from __future__ import annotations

import pytest

from tools.network.idkit.keys import KeyPair
from tools.network.storagekit import credentials, state
from tools.network.storagekit.capability import issue
from tools.network.idkit.sealing import derive_encapsulation_keypair
from tools.network.storagekit.credentials import kem_purpose
from tools.vault.unlock import open_generation_keys, open_generation_keys_for_persona

GENESIS = "1a" * 32
DOMAIN_ID = "2b" * 32
HEADS = ["6f" * 32]
DIGEST = "8b" * 32
HLC0 = (1_800_000_000_000, 0)
KEM_SEED = bytes(range(32))


def _one_grant(kem_seed=KEM_SEED):
    grantor = KeyPair.generate()
    credential, kem_private = credentials.build(
        grantor, GENESIS, kem_seed, HEADS, HLC0
    )
    descriptor, secret = state.generate(
        grantor, GENESIS, DOMAIN_ID, [], HEADS, [], DIGEST
    )
    grant = issue(
        grantor,
        genesis_id=GENESIS,
        domain_id=DOMAIN_ID,
        storage_state_id=descriptor.state_id,
        recipient_credential=credential,
        state_secret=secret,
        state_secret_commitment=descriptor.secret_commitment,
        authority_heads=HEADS,
    )
    return grant, kem_private, descriptor, secret


def test_opens_the_generation_key_into_the_holder_cache_shape():
    grant, kem_private, descriptor, secret = _one_grant()
    opened = open_generation_keys(kem_private, [grant], {descriptor.state_id: descriptor})
    assert opened == {descriptor.state_id: secret}


def test_persona_convenience_derives_the_kem_key_and_opens():
    """The persona-seed entry point derives the same encapsulation key the
    credential was built under, so it opens the same grant."""
    grantor = KeyPair.generate()
    credential, _kem_private = credentials.build(grantor, GENESIS, KEM_SEED, HEADS, HLC0)
    descriptor, secret = state.generate(
        grantor, GENESIS, DOMAIN_ID, [], HEADS, [], DIGEST
    )
    grant = issue(
        grantor, genesis_id=GENESIS, domain_id=DOMAIN_ID,
        storage_state_id=descriptor.state_id, recipient_credential=credential,
        state_secret=secret, state_secret_commitment=descriptor.secret_commitment,
        authority_heads=HEADS,
    )
    opened = open_generation_keys_for_persona(
        KEM_SEED, GENESIS, [grant], {descriptor.state_id: descriptor}
    )
    assert opened == {descriptor.state_id: secret}


def test_a_grant_for_an_absent_descriptor_is_skipped_not_raised():
    grant, kem_private, descriptor, _secret = _one_grant()
    opened = open_generation_keys(kem_private, [grant], {})  # descriptor not held
    assert opened == {}


def test_a_grant_the_key_cannot_open_does_not_deny_the_rest():
    good_grant, good_kem, good_desc, good_secret = _one_grant()
    bad_grant, _other_kem, bad_desc, _bad_secret = _one_grant(kem_seed=bytes(range(100, 132)))
    # good_kem opens good_grant; bad_grant was sealed to a different key, so it
    # fails to open — but must not stop good_grant from loading.
    descriptors = {good_desc.state_id: good_desc, bad_desc.state_id: bad_desc}
    opened = open_generation_keys(good_kem, [bad_grant, good_grant], descriptors)
    assert opened == {good_desc.state_id: good_secret}


def test_wrong_persona_seed_opens_nothing():
    grant, _kem_private, descriptor, _secret = _one_grant()
    wrong = bytes(range(1, 33))  # not KEM_SEED
    opened = open_generation_keys_for_persona(
        wrong, GENESIS, [grant], {descriptor.state_id: descriptor}
    )
    assert opened == {}
