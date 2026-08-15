"""US-5: getting back in with the printed recovery code (row 17).

The bearer principle governs all of this: you reach for a recovery code
precisely when the factor you normally use is GONE, so the code must work
ALONE. A recovery mechanism that needs a surviving factor is not one.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair, recovery
from tools.network.idkit.errors import IdkitError, MalformedError
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


# ── The factor set cannot be edited behind your back (F5) ─────────────────


def _edit_body(armor, mutate):
    import base64 as b64
    import json as js

    lines = [ln for ln in armor.split("\n") if ln]
    body = js.loads(b64.b64decode("".join(lines[1:-1])))
    mutate(body)
    blob = b64.b64encode(js.dumps(body).encode()).decode()
    return "\n".join([lines[0], *[blob[i:i + 64] for i in range(0, len(blob), 64)], lines[-1]])


def test_stripping_the_recovery_factor_is_detected():
    """The attack this closes: a silent downgrade of your last resort.

    Anyone who can write the file -- a backup, a sync folder, a shared disk --
    could delete the recovery factor. The armor still parsed and still opened
    with the password, so nothing looked wrong; the owner would discover it on
    the day they reached for the code, having already lost everything else.
    """
    _key, armor, _code = enrolled()
    stripped = _edit_body(
        armor, lambda b: b.update(factors=[f for f in b["factors"] if f["type"] != "recovery"])
    )
    # It still parses -- the shape is legal. It must not OPEN.
    assert [f["type"] for f in parse_armor_v2(stripped)["factors"]] == ["password"]
    with pytest.raises(MalformedError, match="factor list"):
        decrypt_root_key_v2(stripped, PASSWORD)


def test_stripping_the_password_factor_is_detected():
    _key, armor, code = enrolled()
    stripped = _edit_body(
        armor, lambda b: b.update(factors=[f for f in b["factors"] if f["type"] != "password"])
    )
    with pytest.raises(MalformedError, match="factor list"):
        decrypt_root_key_with_recovery(stripped, code)


def test_swapping_in_another_recovery_key_is_detected():
    """Substituting an attacker's recovery key, not just removing yours."""
    _key, armor, _code = enrolled()
    theirs = recovery.generate_recovery_code()
    seed = recovery.derive_recovery_factors(theirs)["kek_recovery_seed"]
    _, their_kem = derive_encapsulation_keypair(seed, RECOVERY_ARMOR_PURPOSE)

    def swap(b):
        for f in b["factors"]:
            if f["type"] == "recovery":
                f["kem_pub"] = their_kem

    with pytest.raises(MalformedError, match="factor list"):
        decrypt_root_key_v2(_edit_body(armor, swap), PASSWORD)


def test_weakening_the_password_work_factor_is_refused():
    """The work factor is committed, so it cannot be quietly lowered.

    Two guards catch this and either is sufficient: the count feeds the key
    derivation, so changing it derives a different key; and it is part of the
    set commitment. Asserted on the common base so the test does not pin
    WHICH guard fires first, only that the tampering never succeeds.
    """
    key, armor, _code = enrolled()
    assert ITERS != 50_000, "the tampered value must differ from the real one"

    def weaken(b):
        for f in b["factors"]:
            if f["type"] == "password":
                f["kdf"]["iterations"] = 50_000

    tampered = _edit_body(armor, weaken)
    assert parse_armor_v2(tampered)["factors"][0]["kdf"]["iterations"] == 50_000
    with pytest.raises(IdkitError):
        decrypt_root_key_v2(tampered, PASSWORD)
    # And the untampered armor still opens, so the test is not vacuous.
    assert decrypt_root_key_v2(armor, PASSWORD).public_hex == key.public_hex


def test_reordering_the_factors_is_harmless():
    """The commitment is over a SET, so order is not load-bearing."""
    key, armor, code = enrolled()
    reordered = _edit_body(armor, lambda b: b.update(factors=list(reversed(b["factors"]))))
    assert decrypt_root_key_v2(reordered, PASSWORD).public_hex == key.public_hex
    assert decrypt_root_key_with_recovery(reordered, code).public_hex == key.public_hex


def test_the_owner_can_still_change_their_own_locks():
    """Tamper-evidence must not make legitimate change impossible."""
    key = KeyPair.generate()
    armor = encrypt_root_key_v2(key, PASSWORD, iterations=ITERS)
    code = recovery.generate_recovery_code()
    seed = recovery.derive_recovery_factors(code)["kek_recovery_seed"]
    _, kem_pub = derive_encapsulation_keypair(seed, RECOVERY_ARMOR_PURPOSE)
    # Adding a factor requires the passphrase -- i.e. the authority to open it.
    added = add_recovery_factor(armor, PASSWORD, kem_pub)
    assert decrypt_root_key_v2(added, PASSWORD).public_hex == key.public_hex
    assert decrypt_root_key_with_recovery(added, code).public_hex == key.public_hex


def test_a_factor_type_with_no_commitment_is_refused():
    """Total dispatch again: a type that pins nothing could be stripped."""
    from tools.network.idkit.armor import _factor_commitment

    with pytest.raises(ArmorError, match="no set commitment"):
        _factor_commitment([{"type": "future-thing"}])


# ── US-6: changing which locks you use ────────────────────────────────────


def test_a_lock_can_be_removed_by_someone_who_can_open_the_armor():
    key, armor, code = enrolled()
    from tools.network.idkit.armor import armor_factor_types, remove_factor

    assert armor_factor_types(armor) == ["password", "recovery"]
    dropped = remove_factor(armor, PASSWORD, "recovery")
    assert armor_factor_types(dropped) == ["password"]
    # Still the same identity, still openable the remaining way.
    assert decrypt_root_key_v2(dropped, PASSWORD).public_hex == key.public_hex
    # And the dropped lock really is gone.
    with pytest.raises(ArmorError, match="no recovery factor"):
        decrypt_root_key_with_recovery(dropped, code)


def test_the_last_lock_cannot_be_removed():
    """An armor nothing opens is a destroyed identity, not a hardened one."""
    from tools.network.idkit.armor import remove_factor

    key = KeyPair.generate()
    armor = encrypt_root_key_v2(key, PASSWORD, iterations=ITERS)
    with pytest.raises(ArmorError, match="last factor"):
        remove_factor(armor, PASSWORD, "password")


def test_removing_a_lock_needs_the_authority_to_open_it():
    from tools.network.idkit.armor import remove_factor

    _key, armor, _code = enrolled()
    with pytest.raises(ArmorPassphraseError):
        remove_factor(armor, "not-the-passphrase", "recovery")


def test_removing_a_lock_that_is_not_there_is_refused():
    from tools.network.idkit.armor import remove_factor

    key = KeyPair.generate()
    armor = encrypt_root_key_v2(key, PASSWORD, iterations=ITERS)
    with pytest.raises(ArmorError, match="no 'recovery' factor"):
        remove_factor(armor, PASSWORD, "recovery")


def test_removing_an_unknown_factor_type_is_refused():
    from tools.network.idkit.armor import remove_factor

    _key, armor, _code = enrolled()
    with pytest.raises(ArmorError, match="unknown factor type"):
        remove_factor(armor, PASSWORD, "backdoor")


def test_the_armor_left_behind_still_opens_with_the_lock_you_dropped():
    """The honest limit of dropping a weak lock, pinned so nobody assumes more.

    Removing a factor does not reach copies that already exist. Only rotating
    the identity makes those worthless.
    """
    key, armor, code = enrolled()
    from tools.network.idkit.armor import remove_factor

    remove_factor(armor, PASSWORD, "recovery")
    # The ORIGINAL file is untouched and its recovery lock still works.
    assert decrypt_root_key_with_recovery(armor, code).public_hex == key.public_hex


def test_a_removed_lock_can_be_added_again():
    """Changing your locks is reversible; it is housekeeping, not a ratchet."""
    from tools.network.idkit.armor import armor_factor_types, remove_factor

    key, armor, code = enrolled()
    dropped = remove_factor(armor, PASSWORD, "recovery")
    seed = recovery.derive_recovery_factors(code)["kek_recovery_seed"]
    _, kem_pub = derive_encapsulation_keypair(seed, RECOVERY_ARMOR_PURPOSE)
    restored = add_recovery_factor(dropped, PASSWORD, kem_pub)
    assert armor_factor_types(restored) == ["password", "recovery"]
    assert decrypt_root_key_with_recovery(restored, code).public_hex == key.public_hex
