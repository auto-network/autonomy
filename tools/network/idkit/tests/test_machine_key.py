"""Per-machine operating key derivation (auto-b6fee)."""

from __future__ import annotations

import pytest

from tools.network.idkit import verify_signature
from tools.network.idkit.persona import (
    derive_machine_key,
    derive_persona,
    mint_machine_id,
    MACHINE_KEY_SALT,
    PERSONA_SALT,
)
from tools.network.idkit.errors import MalformedError

ROOT = bytes(range(32))
GEN = "1a" * 32


def test_mint_is_64_hex_and_random():
    a, b = mint_machine_id(), mint_machine_id()
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)
    assert a != b


def test_deterministic_for_root_and_id():
    mid = mint_machine_id()
    assert derive_machine_key(ROOT, mid).public_hex == derive_machine_key(ROOT, mid).public_hex


def test_distinct_per_machine_and_per_root():
    m1, m2 = mint_machine_id(), mint_machine_id()
    assert derive_machine_key(ROOT, m1).public_hex != derive_machine_key(ROOT, m2).public_hex
    other_root = bytes(range(1, 33))
    assert derive_machine_key(ROOT, m1).public_hex != derive_machine_key(other_root, m1).public_hex


def test_independent_from_persona_even_if_ids_collide():
    # Same 64-hex string used as both a genesis id and a machine id: the distinct
    # salt makes the two keys independent, so this is safe by construction.
    same = "ab" * 32
    assert derive_machine_key(ROOT, same).public_hex != derive_persona(ROOT, same).public_hex
    assert MACHINE_KEY_SALT != PERSONA_SALT


def test_authenticates_by_signing():
    kp = derive_machine_key(ROOT, mint_machine_id())
    sig = kp.sign_hex(b"fleet-channel-hello")
    verify_signature(kp.public_hex, sig, b"fleet-channel-hello")


def test_any_root_holder_derives_any_machine_key_from_its_public_id():
    # The fleet-membership property, made explicit: given the public id, the root
    # holder derives the same key. (Correct under the mutual-trust model.)
    mid = mint_machine_id()
    assert derive_machine_key(ROOT, mid).private_hex == derive_machine_key(ROOT, mid).private_hex


def test_rejects_bad_inputs():
    with pytest.raises(MalformedError):
        derive_machine_key(b"short", mint_machine_id())
    with pytest.raises(MalformedError):
        derive_machine_key(ROOT, "not-64-hex")
