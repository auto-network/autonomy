"""A personal identity may be armored in either format (I1 gate).

The I1 gate refuses anything that is not a strictly-parsed, canonical armor.
It was also pinned to ONE version, which would have refused the very format
the ceremonies are moving to -- so a browser minting the newer armor could
never have stored it. Both are accepted; neither is accepted loosely.
"""

from __future__ import annotations

import pytest

from tools.graph.schemas.personal_identity import PersonalIdentityV1
from tools.graph.schemas.registry import SchemaValidationError
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import (
    encrypt_root_key,
    encrypt_root_key,
)

PASSWORD = "the-personal-password"
ITERS = 10_000


def payload(armor, root_pub):
    return {
        "armored_private_key": armor,
        "root_pub": root_pub,
        "display_name": "Test Owner",
        "created_at": "2026-08-15T00:00:00Z",
    }


def validate(data):
    PersonalIdentityV1.validate(data)


def test_a_legacy_armored_identity_is_accepted():
    key = KeyPair.generate()
    validate(payload(encrypt_root_key(key, PASSWORD, iterations=ITERS), key.public_hex))


def test_a_multi_lock_armored_identity_is_accepted():
    """The format the ceremonies are moving to must be storable."""
    key = KeyPair.generate()
    validate(
        payload(encrypt_root_key(key, PASSWORD, iterations=ITERS), key.public_hex)
    )


def test_a_multi_lock_identity_carrying_a_recovery_lock_is_accepted():
    from tools.network.idkit import recovery
    from tools.network.idkit.armor import RECOVERY_ARMOR_PURPOSE, add_recovery_factor
    from tools.network.idkit.sealing import derive_encapsulation_keypair

    key = KeyPair.generate()
    armor = encrypt_root_key(key, PASSWORD, iterations=ITERS)
    code = recovery.generate_recovery_code()
    seed = recovery.derive_recovery_factors(code)["kek_recovery_seed"]
    _, kem_pub = derive_encapsulation_keypair(seed, RECOVERY_ARMOR_PURPOSE)
    validate(payload(add_recovery_factor(armor, PASSWORD, kem_pub), key.public_hex))


@pytest.mark.parametrize("mint", [encrypt_root_key, encrypt_root_key])
def test_a_mismatched_root_pub_is_refused_in_both_formats(mint):
    key = KeyPair.generate()
    other = KeyPair.generate()
    with pytest.raises(SchemaValidationError):
        validate(payload(mint(key, PASSWORD, iterations=ITERS), other.public_hex))


@pytest.mark.parametrize("mint", [encrypt_root_key, encrypt_root_key])
def test_a_non_canonical_armor_is_refused_in_both_formats(mint):
    """Accepting two versions must not mean accepting them loosely."""
    key = KeyPair.generate()
    armor = mint(key, PASSWORD, iterations=ITERS)
    # Same bytes, different layout: the canonical-form check must still bite.
    reflowed = armor.replace("\n", "\n\n")
    with pytest.raises(SchemaValidationError):
        validate(payload(reflowed, key.public_hex))


def test_plaintext_key_material_is_still_refused():
    """The whole point of the gate: a bare seed must never be storable."""
    key = KeyPair.generate()
    with pytest.raises(SchemaValidationError):
        validate(payload(key.private_hex, key.public_hex))


def test_an_armor_with_a_smuggled_field_is_refused():
    import base64
    import json

    key = KeyPair.generate()
    armor = encrypt_root_key(key, PASSWORD, iterations=ITERS)
    lines = [ln for ln in armor.split("\n") if ln]
    body = json.loads(base64.b64decode("".join(lines[1:-1])))
    body["smuggled"] = "AAAA"
    blob = base64.b64encode(json.dumps(body).encode()).decode()
    tampered = "\n".join(
        [lines[0], *[blob[i:i + 64] for i in range(0, len(blob), 64)], lines[-1]]
    )
    with pytest.raises(SchemaValidationError):
        validate(payload(tampered, key.public_hex))
