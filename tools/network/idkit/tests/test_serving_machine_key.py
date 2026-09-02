"""Per-(organization, machine) serving key derivation (auto-e2ufw).

The identity a machine shows an untrusted relay when serving under one org.
It must be deterministic, unlinkable ACROSS organizations, and independent
of both the persona and the fleet machine key.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import verify_signature
from tools.network.idkit.persona import (
    SERVING_MACHINE_KEY_SALT,
    MACHINE_KEY_SALT,
    PERSONA_SALT,
    derive_machine_key,
    derive_persona,
    derive_serving_machine_key,
    mint_machine_id,
)
from tools.network.idkit.errors import MalformedError

ROOT = bytes(range(32))
GEN_A = "1a" * 32
GEN_B = "2b" * 32


def test_deterministic_for_root_org_and_machine():
    mid = mint_machine_id()
    assert (derive_serving_machine_key(ROOT, GEN_A, mid).public_hex
            == derive_serving_machine_key(ROOT, GEN_A, mid).public_hex)


def test_unlinkable_across_organizations():
    # THE property: one machine under two orgs yields unrelated keys, so the
    # relay cannot correlate the machine across organizations.
    mid = mint_machine_id()
    assert (derive_serving_machine_key(ROOT, GEN_A, mid).public_hex
            != derive_serving_machine_key(ROOT, GEN_B, mid).public_hex)


def test_distinct_per_machine_and_per_root():
    m1, m2 = mint_machine_id(), mint_machine_id()
    assert (derive_serving_machine_key(ROOT, GEN_A, m1).public_hex
            != derive_serving_machine_key(ROOT, GEN_A, m2).public_hex)
    other = bytes(range(1, 33))
    assert (derive_serving_machine_key(ROOT, GEN_A, m1).public_hex
            != derive_serving_machine_key(other, GEN_A, m1).public_hex)


def test_independent_from_persona_and_fleet_key():
    mid = mint_machine_id()
    serving = derive_serving_machine_key(ROOT, GEN_A, mid).public_hex
    # Differs from the org's persona and from the machine's fleet key.
    assert serving != derive_persona(ROOT, GEN_A).public_hex
    assert serving != derive_machine_key(ROOT, mid).public_hex
    # And the three salts are mutually distinct by construction.
    assert len({SERVING_MACHINE_KEY_SALT, MACHINE_KEY_SALT, PERSONA_SALT}) == 3


def test_no_axis_collision_when_genesis_equals_machine():
    # A 64-hex value used as BOTH genesis and machine id must not fold the
    # two axes together: swapping them changes the key. The NUL-separated
    # info makes (X, Y) and (Y, X) distinct.
    x, y = "ab" * 32, "cd" * 32
    assert (derive_serving_machine_key(ROOT, x, y).public_hex
            != derive_serving_machine_key(ROOT, y, x).public_hex)


def test_signature_verifies_under_derived_public_key():
    kp = derive_serving_machine_key(ROOT, GEN_A, mint_machine_id())
    sig = kp.sign_hex(b"serving-hello-core")
    verify_signature(kp.public_hex, sig, b"serving-hello-core")


@pytest.mark.parametrize("bad", ["", "xyz", "ab" * 31, "AB" * 32, "gg" * 32])
def test_malformed_ids_rejected(bad):
    good = "1a" * 32
    with pytest.raises(MalformedError):
        derive_serving_machine_key(ROOT, bad, good)
    with pytest.raises(MalformedError):
        derive_serving_machine_key(ROOT, good, bad)


def test_short_root_rejected():
    with pytest.raises(MalformedError):
        derive_serving_machine_key(b"short", GEN_A, "1a" * 32)
