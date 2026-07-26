"""Persona derivation: deterministic, org-scoped, unlinkable, fail-closed."""

from __future__ import annotations

import pytest

from tools.network.idkit import (
    KeyPair,
    MalformedError,
    SignatureError,
    derive_persona,
    verify_signature,
)

SEED = bytes(range(32))
OTHER_SEED = bytes(range(1, 33))
GID = "1f" * 32
OTHER_GID = "2e" * 32


def test_derive_is_deterministic():
    assert derive_persona(SEED, GID).public_hex == derive_persona(SEED, GID).public_hex


def test_distinct_genesis_ids_yield_unrelated_personas():
    root = KeyPair.from_private_hex(SEED.hex())
    p1 = derive_persona(SEED, GID)
    p2 = derive_persona(SEED, OTHER_GID)
    assert p1.public_hex != p2.public_hex
    assert p1.public_hex != root.public_hex
    assert p2.public_hex != root.public_hex
    assert p1.private_hex != p2.private_hex
    assert p1.private_hex != root.private_hex


def test_distinct_seeds_yield_distinct_personas():
    assert derive_persona(SEED, GID).public_hex != derive_persona(OTHER_SEED, GID).public_hex


def test_persona_signs_and_verifies():
    persona = derive_persona(SEED, GID)
    sig = persona.sign_hex(b"ledger-event")
    assert verify_signature(persona.public_hex, sig, b"ledger-event") is None
    other = derive_persona(SEED, OTHER_GID)
    with pytest.raises(SignatureError):
        verify_signature(other.public_hex, sig, b"ledger-event")


@pytest.mark.parametrize(
    "bad_seed",
    [
        b"",  # empty
        bytes(31),  # too short
        bytes(33),  # too long
        SEED.hex(),  # str, not bytes
        None,
        1234,
    ],
)
def test_rejects_malformed_seed(bad_seed):
    with pytest.raises(MalformedError):
        derive_persona(bad_seed, GID)


@pytest.mark.parametrize(
    "bad_gid",
    [
        "",  # empty
        "ab" * 31,  # too short
        "ab" * 33,  # too long
        "AB" * 32,  # uppercase
        "zz" * 32,  # not hex
        # Whitespace-padded 64-char forms: bytes.fromhex would accept them,
        # and the derivation anchors on the STRING — a padded variant must
        # never mint a distinct persona.
        "  " + "1f" * 31,
        "1f" * 31 + " \n",
        None,
        1234,
    ],
)
def test_rejects_malformed_genesis_id(bad_gid):
    with pytest.raises(MalformedError):
        derive_persona(SEED, bad_gid)


def test_frozen_derivation_vector():
    # Locks salt, KDF, hash, and the genesis_id string anchor: any change
    # to the construction re-keys every persona and must fail here first.
    persona = derive_persona(bytes(32), "a" * 64)
    assert (
        persona.public_hex
        == "8534182dfea05c278ff60bac38c37154db03210f81767b69b76747693e13e99b"
    )
