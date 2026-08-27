"""The v3 armor's optional recovery slot (operator-ruled 2026-08-27).

A recovery code opens the v3 root ALONE, exactly parallel to the v2 recovery
factor — but as an OPTIONAL field on the factor-policy envelope, outside the
policy tree (recovery is the emergency floor, never a day-to-day opener),
covered by the envelope signature so it cannot be stripped or swapped. No armor
version bump: parse accepts an envelope with or without the field.

Design of record: graph://fd418706-97e. Leverages the existing, already-built
primitives (idkit/recovery.py derive_recovery_factors, the RECOVERY_ARMOR
sealing), which the v2 path uses verbatim, so one printed code works identically
against v2 and v3.
"""

from __future__ import annotations

import copy

import pytest

from tools.network.idkit.armor import RECOVERY_ARMOR_PURPOSE
from tools.network.idkit.keys import KeyPair
from tools.network.idkit.recovery import (
    derive_recovery_factors,
    generate_recovery_code,
    recovery_signing_key,
)
from tools.network.idkit.root_factor_policy import (
    RootFactorPolicyError,
    add_recovery_slot,
    build_envelope,
    create_password_factor,
    factor_leaf,
    open_envelope,
    open_root_with_recovery,
    parse_envelope,
    recovery_recipient_public_key,
)
from tools.network.idkit.sealing import derive_encapsulation_keypair


def _armor():
    root = KeyPair.generate()
    password, seed = create_password_factor(
        root.public_hex, "pw.main", "alpha-alpha", iterations=10_000,
    )
    envelope = build_envelope(
        root, generation=1, factors=[password],
        access={"pw.main": seed}, policy=factor_leaf("pw.main"),
    )
    return root, envelope, {"pw.main": seed}


def test_envelope_without_recovery_still_parses_backward_compatible():
    root, envelope, seeds = _armor()
    assert "recovery" not in envelope
    parse_envelope(envelope)  # unchanged behavior
    assert open_envelope(envelope, seeds).public_hex == root.public_hex


def test_recovery_code_opens_the_root_alone():
    root, envelope, seeds = _armor()
    code = generate_recovery_code()
    factors = derive_recovery_factors(code)
    recipient = recovery_recipient_public_key(code)
    with_recovery = add_recovery_slot(
        envelope,
        root_seed=bytes.fromhex(root.private_hex),
        recovery_recipient_pub=recipient,
        recovery_pub=factors["recovery_pub"],
    )
    # the field is present, carries the signing half, and re-parses
    assert with_recovery["recovery"]["recipient_public_key"] == recipient
    assert with_recovery["recovery"]["recovery_pub"] == factors["recovery_pub"]
    parse_envelope(with_recovery)
    # the day-to-day factor still opens it, unchanged
    assert open_envelope(with_recovery, seeds).public_hex == root.public_hex
    # and the printed code opens the root on its own
    recovered = open_root_with_recovery(with_recovery, code)
    assert recovered.public_hex == root.public_hex
    assert recovered.private_hex == root.private_hex


def test_recovery_pub_matches_the_signing_key_derivation():
    # the declared recovery_pub is the code's Ed25519 signing key (for future
    # rotation), not the KEM recipient — they are distinct halves of one code
    code = generate_recovery_code()
    factors = derive_recovery_factors(code)
    assert factors["recovery_pub"] == recovery_signing_key(code).public_hex
    assert recovery_recipient_public_key(code) != factors["recovery_pub"]


def test_signature_covers_recovery_so_it_cannot_be_stripped_or_swapped():
    root, envelope, seeds = _armor()
    code = generate_recovery_code()
    with_recovery = add_recovery_slot(
        envelope, root_seed=bytes.fromhex(root.private_hex),
        recovery_recipient_pub=recovery_recipient_public_key(code),
        recovery_pub=derive_recovery_factors(code)["recovery_pub"],
    )
    # stripping the recovery field breaks the signature
    stripped = copy.deepcopy(with_recovery)
    del stripped["recovery"]
    with pytest.raises(RootFactorPolicyError):
        parse_envelope(stripped)
    # swapping in an attacker's recovery recipient breaks the signature
    attacker = generate_recovery_code()
    swapped = copy.deepcopy(with_recovery)
    swapped["recovery"]["recipient_public_key"] = recovery_recipient_public_key(attacker)
    with pytest.raises(RootFactorPolicyError):
        parse_envelope(swapped)


def test_a_wrong_code_does_not_open_the_recovery_slot():
    root, envelope, seeds = _armor()
    code = generate_recovery_code()
    with_recovery = add_recovery_slot(
        envelope, root_seed=bytes.fromhex(root.private_hex),
        recovery_recipient_pub=recovery_recipient_public_key(code),
        recovery_pub=derive_recovery_factors(code)["recovery_pub"],
    )
    with pytest.raises(RootFactorPolicyError):
        open_root_with_recovery(with_recovery, generate_recovery_code())


def test_open_with_recovery_refuses_an_envelope_without_a_slot():
    root, envelope, seeds = _armor()
    with pytest.raises(RootFactorPolicyError):
        open_root_with_recovery(envelope, generate_recovery_code())


def test_add_recovery_slot_refuses_to_replace_an_existing_one():
    # replacement requires the old code AND root, and is NOT built yet
    # (operator ruling): a second add is refused outright.
    root, envelope, seeds = _armor()
    code = generate_recovery_code()
    with_recovery = add_recovery_slot(
        envelope, root_seed=bytes.fromhex(root.private_hex),
        recovery_recipient_pub=recovery_recipient_public_key(code),
        recovery_pub=derive_recovery_factors(code)["recovery_pub"],
    )
    with pytest.raises(RootFactorPolicyError):
        add_recovery_slot(
            with_recovery, root_seed=bytes.fromhex(root.private_hex),
            recovery_recipient_pub=recovery_recipient_public_key(generate_recovery_code()),
            recovery_pub=derive_recovery_factors(generate_recovery_code())["recovery_pub"],
        )
