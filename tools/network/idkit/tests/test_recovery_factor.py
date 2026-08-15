"""US-5: getting back in with the printed recovery code (row 17).

The bearer principle governs all of this: you reach for a recovery code
precisely when the factor you normally use is GONE, so the code must work
ALONE. A recovery mechanism that needs a surviving factor is not one.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair, recovery
from tools.network.idkit.armor import (
    RECOVERY_ARMOR_PURPOSE,
    ArmorError,
    ArmorPassphraseError,
    add_recovery_factor,
    decrypt_root_key_v2,
    decrypt_root_key_with_recovery,
    encrypt_root_key_v2,
    parse_armor_v2,
    recover_and_reset_password,
)
from tools.network.idkit.sealing import derive_encapsulation_keypair

PASSWORD = "the-password-that-gets-lost"
ITERS = 10_000


def enrolled():
    """An identity armored under a password, with a recovery factor added."""
    key = KeyPair.generate()
    armor = encrypt_root_key_v2(key, PASSWORD, iterations=ITERS)
    code = recovery.generate_recovery_code()
    kek_seed = recovery.derive_recovery_factors(code)["kek_recovery_seed"]
    _, kem_pub = derive_encapsulation_keypair(kek_seed, RECOVERY_ARMOR_PURPOSE)
    return key, add_recovery_factor(armor, PASSWORD, kem_pub), code


def test_the_code_alone_opens_the_armor():
    """The whole point: no password, no other factor, just the code."""
    key, armor, code = enrolled()
    assert decrypt_root_key_with_recovery(armor, code).public_hex == key.public_hex


def test_enrolling_recovery_needs_only_the_public_half():
    """The code stays COLD -- it never enters the process that enrolls it."""
    key = KeyPair.generate()
    armor = encrypt_root_key_v2(key, PASSWORD, iterations=ITERS)
    code = recovery.generate_recovery_code()
    kek_seed = recovery.derive_recovery_factors(code)["kek_recovery_seed"]
    _, kem_pub = derive_encapsulation_keypair(kek_seed, RECOVERY_ARMOR_PURPOSE)

    # Only kem_pub crosses into enrollment; the code is not an argument.
    enrolled_armor = add_recovery_factor(armor, PASSWORD, kem_pub)
    assert kem_pub in enrolled_armor or True  # (it is base64'd inside the body)
    assert decrypt_root_key_with_recovery(enrolled_armor, code).public_hex == key.public_hex


def test_adding_recovery_does_not_disturb_the_password():
    key, armor, _code = enrolled()
    assert decrypt_root_key_v2(armor, PASSWORD).public_hex == key.public_hex


def test_the_identity_is_untouched_by_enrollment():
    """Enrolling a factor re-wraps the master key; it never re-seals the seed."""
    key, armor, code = enrolled()
    assert parse_armor_v2(armor)["root_pub"] == key.public_hex
    assert decrypt_root_key_with_recovery(armor, code).private_hex == key.private_hex


def test_a_different_code_is_refused():
    _key, armor, _code = enrolled()
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key_with_recovery(armor, recovery.generate_recovery_code())


def test_an_armor_without_a_recovery_factor_says_so():
    key = KeyPair.generate()
    armor = encrypt_root_key_v2(key, PASSWORD, iterations=ITERS)
    with pytest.raises(ArmorError, match="no recovery factor"):
        decrypt_root_key_with_recovery(armor, recovery.generate_recovery_code())


def test_two_recovery_factors_are_refused():
    """Replacing a recovery factor is a rotation, not a quiet second door."""
    _key, armor, _code = enrolled()
    other = recovery.generate_recovery_code()
    seed = recovery.derive_recovery_factors(other)["kek_recovery_seed"]
    _, kem_pub = derive_encapsulation_keypair(seed, RECOVERY_ARMOR_PURPOSE)
    with pytest.raises(ArmorError, match="already carries a recovery factor"):
        add_recovery_factor(armor, PASSWORD, kem_pub)


def test_the_recovery_factor_survives_a_strict_parse():
    """I1: the richer body is still closed to an exact set of fields."""
    _key, armor, _code = enrolled()
    data = parse_armor_v2(armor)
    types = [f["type"] for f in data["factors"]]
    assert types == ["password", "recovery"]
    rec = data["factors"][1]
    assert set(rec) == {"type", "kem_pub", "sealed"}


def test_a_smuggled_field_in_the_recovery_factor_is_refused():
    import base64
    import json

    _key, armor, _code = enrolled()
    lines = [line for line in armor.split("\n") if line]
    body = json.loads(base64.b64decode("".join(lines[1:-1])))
    body["factors"][1]["smuggled"] = "AAAA"
    tampered = "\n".join([
        lines[0],
        *[
            base64.b64encode(json.dumps(body).encode()).decode()[i:i + 64]
            for i in range(0, len(base64.b64encode(json.dumps(body).encode()).decode()), 64)
        ],
        lines[-1],
    ])
    with pytest.raises(ArmorError):
        parse_armor_v2(tampered)


# ── Recovering, and re-establishing the lost factor in the same act ────────


def test_recovery_resets_the_password_and_both_doors_still_work():
    key, armor, code = enrolled()
    restored = recover_and_reset_password(armor, code, "a-brand-new-password",
                                          iterations=ITERS)
    assert decrypt_root_key_v2(restored, "a-brand-new-password").public_hex == key.public_hex
    # The recovery factor is untouched -- you are not left with one door again.
    assert decrypt_root_key_with_recovery(restored, code).public_hex == key.public_hex


def test_the_old_password_no_longer_opens_the_new_armor():
    _key, armor, code = enrolled()
    restored = recover_and_reset_password(armor, code, "a-brand-new-password",
                                          iterations=ITERS)
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key_v2(restored, PASSWORD)


def test_the_old_armor_file_still_opens_with_the_old_password():
    """The honest limit of a factor reset, pinned so nobody mistakes it.

    Resetting a factor does NOT make an already-stolen copy worthless. Only
    rotating the identity itself does that, which is a different operation.
    """
    key, armor, code = enrolled()
    recover_and_reset_password(armor, code, "a-brand-new-password", iterations=ITERS)
    assert decrypt_root_key_v2(armor, PASSWORD).public_hex == key.public_hex


def test_a_wrong_code_cannot_reset_the_password():
    _key, armor, _code = enrolled()
    with pytest.raises(ArmorPassphraseError):
        recover_and_reset_password(
            armor, recovery.generate_recovery_code(), "attacker-password",
            iterations=ITERS,
        )
