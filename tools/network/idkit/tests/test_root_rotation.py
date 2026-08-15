"""US-8: rotating a personal root — making a stolen copy worthless.

The threat this answers is the one recovery cannot: someone already has a copy
of your armor and knows the password. You cannot un-copy it. What you can do
is stop being that key.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair, recovery
from tools.network.idkit.armor import (
    decrypt_root_key_v2,
    encrypt_root_key_v2,
)
from tools.network.idkit.errors import MalformedError, SignatureError
from tools.network.idkit.root_rotation import (
    make_rotation,
    resolve_current_root,
    rotation_input,
    rotation_recovery_input,
    verify_rotation,
)

NOW = 1_800_000_000
ITERS = 10_000


@pytest.fixture
def identity():
    origin = KeyPair.generate()
    code = recovery.generate_recovery_code()
    return origin, code, recovery.recovery_signing_key(code)


def rotate(origin_pub, seq, old, new, rec):
    return make_rotation(
        origin_pub=origin_pub, seq=seq, old_root=old, new_root=new,
        recovery_root=rec, rotated_at=NOW,
    )


# ── The point of the whole thing ──────────────────────────────────────────


def test_a_stolen_armor_opens_a_key_that_is_no_longer_you(identity):
    origin, _code, rec = identity
    stolen = encrypt_root_key_v2(origin, "the-password-they-know", iterations=ITERS)

    new_root = KeyPair.generate()
    history = [rotate(origin.public_hex, 1, origin, new_root, rec)]

    # The thief's copy still opens. That is expected and unavoidable.
    assert decrypt_root_key_v2(stolen, "the-password-they-know").public_hex == origin.public_hex
    # It just is not the current identity any more.
    current = resolve_current_root(origin.public_hex, history, recovery_pub=rec.public_hex)
    assert current == new_root.public_hex
    assert current != origin.public_hex


# ── Split authority: neither half can act alone ───────────────────────────


def test_the_old_root_alone_cannot_rotate(identity):
    """A thief holding your armor must not be able to rotate you out."""
    origin, _code, rec = identity
    forged = rotate(origin.public_hex, 1, origin, KeyPair.generate(), KeyPair.generate())
    with pytest.raises(SignatureError):
        verify_rotation(forged, recovery_pub=rec.public_hex)


def test_the_recovery_key_alone_cannot_rotate(identity):
    """Someone who found the printed code cannot rotate without the old root."""
    origin, _code, rec = identity
    impostor = KeyPair.generate()
    forged = rotate(origin.public_hex, 1, impostor, KeyPair.generate(), rec)
    # The record is internally consistent, but it does not start from the
    # identity being rotated, so the lineage refuses it.
    with pytest.raises(SignatureError):
        resolve_current_root(origin.public_hex, [forged], recovery_pub=rec.public_hex)


def test_the_new_key_must_prove_it_exists(identity):
    origin, _code, rec = identity
    record = rotate(origin.public_hex, 1, origin, KeyPair.generate(), rec)
    record["new_sig"] = rotate(
        origin.public_hex, 1, origin, KeyPair.generate(), rec
    )["new_sig"]
    with pytest.raises(SignatureError):
        verify_rotation(record, recovery_pub=rec.public_hex)


# ── Domain separation and replay ──────────────────────────────────────────


def test_the_two_signatures_cannot_substitute_for_each_other(identity):
    origin, _code, rec = identity
    new_root = KeyPair.generate()
    args = (origin.public_hex, 1, origin.public_hex, new_root.public_hex)
    assert rotation_input(*args) != rotation_recovery_input(*args)

    record = rotate(origin.public_hex, 1, origin, new_root, rec)
    # The recovery key signs the AUTHORISING domain instead of its own.
    record["recovery_sig"] = rec.sign_hex(rotation_input(*args))
    with pytest.raises(SignatureError):
        verify_rotation(record, recovery_pub=rec.public_hex)


def test_a_co_signature_cannot_be_re_pointed_at_another_successor(identity):
    """Approval to hand your identity to one key must not install a different one."""
    origin, _code, rec = identity
    intended, attacker = KeyPair.generate(), KeyPair.generate()
    approved = rotate(origin.public_hex, 1, origin, intended, rec)

    hijacked = rotate(origin.public_hex, 1, origin, attacker, KeyPair.generate())
    hijacked["recovery_sig"] = approved["recovery_sig"]
    with pytest.raises(SignatureError):
        verify_rotation(hijacked, recovery_pub=rec.public_hex)


def test_a_co_signature_cannot_be_replayed_at_a_later_step(identity):
    origin, _code, rec = identity
    first = KeyPair.generate()
    step1 = rotate(origin.public_hex, 1, origin, first, rec)
    second = KeyPair.generate()
    step2 = rotate(origin.public_hex, 2, first, second, KeyPair.generate())
    step2["recovery_sig"] = step1["recovery_sig"]  # the seq is bound in
    with pytest.raises(SignatureError):
        verify_rotation(step2, recovery_pub=rec.public_hex)


def test_a_record_from_another_identity_is_refused(identity):
    origin, _code, rec = identity
    stranger = KeyPair.generate()
    foreign = rotate(stranger.public_hex, 1, stranger, KeyPair.generate(), rec)
    with pytest.raises(SignatureError, match="different identity"):
        resolve_current_root(origin.public_hex, [foreign], recovery_pub=rec.public_hex)


# ── The lineage ───────────────────────────────────────────────────────────


def test_no_history_means_the_origin_is_still_current(identity):
    origin, _code, rec = identity
    assert resolve_current_root(
        origin.public_hex, [], recovery_pub=rec.public_hex
    ) == origin.public_hex


def test_a_chain_of_rotations_resolves_to_the_last_one(identity):
    origin, _code, rec = identity
    k1, k2, k3 = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    history = [
        rotate(origin.public_hex, 1, origin, k1, rec),
        rotate(origin.public_hex, 2, k1, k2, rec),
        rotate(origin.public_hex, 3, k2, k3, rec),
    ]
    assert resolve_current_root(
        origin.public_hex, history, recovery_pub=rec.public_hex
    ) == k3.public_hex


def test_a_dropped_step_is_refused(identity):
    """Skipping a step would let an intermediate key look current."""
    origin, _code, rec = identity
    k1, k2 = KeyPair.generate(), KeyPair.generate()
    history = [
        rotate(origin.public_hex, 1, origin, k1, rec),
        rotate(origin.public_hex, 2, k1, k2, rec),
    ]
    with pytest.raises(SignatureError, match="out of order"):
        resolve_current_root(
            origin.public_hex, [history[1]], recovery_pub=rec.public_hex
        )


def test_reordered_steps_are_refused(identity):
    origin, _code, rec = identity
    k1, k2 = KeyPair.generate(), KeyPair.generate()
    history = [
        rotate(origin.public_hex, 1, origin, k1, rec),
        rotate(origin.public_hex, 2, k1, k2, rec),
    ]
    with pytest.raises(SignatureError):
        resolve_current_root(
            origin.public_hex, list(reversed(history)), recovery_pub=rec.public_hex
        )


def test_a_fork_cannot_become_the_trunk(identity):
    """Two successors from one key: the second does not follow the first."""
    origin, _code, rec = identity
    branch_a, branch_b = KeyPair.generate(), KeyPair.generate()
    history = [
        rotate(origin.public_hex, 1, origin, branch_a, rec),
        rotate(origin.public_hex, 2, origin, branch_b, rec),
    ]
    with pytest.raises(SignatureError, match="does not follow"):
        resolve_current_root(origin.public_hex, history, recovery_pub=rec.public_hex)


# ── Shape ─────────────────────────────────────────────────────────────────


def test_rotating_to_the_same_key_is_refused(identity):
    origin, _code, rec = identity
    with pytest.raises(MalformedError, match="different key"):
        rotate(origin.public_hex, 1, origin, origin, rec)


def test_an_extra_field_is_refused(identity):
    origin, _code, rec = identity
    record = rotate(origin.public_hex, 1, origin, KeyPair.generate(), rec)
    record["smuggled"] = "x"
    with pytest.raises(MalformedError):
        verify_rotation(record, recovery_pub=rec.public_hex)


def test_the_recovery_key_comes_from_the_printed_code(identity):
    """The co-signer is not a separate thing to look after; it IS the code."""
    _origin, code, rec = identity
    assert recovery.derive_recovery_factors(code)["recovery_pub"] == rec.public_hex
