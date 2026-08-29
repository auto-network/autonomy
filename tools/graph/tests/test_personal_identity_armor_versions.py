"""A personal identity's armor must be a version-3 root factor policy (I1).

The I1 gate refuses anything that is not a strictly-parsed, canonical
version-3 armored envelope. The retired version-2 multi-lock armor is
refused with the facade's retired-format error — no write path stores one.
"""

from __future__ import annotations

import base64

import pytest

from tools.graph.schemas.personal_identity import PersonalIdentityV1
from tools.graph.schemas.registry import SchemaValidationError
from tools.network.idkit import KeyPair
from tools.network.idkit.root_factor_policy import (
    add_recovery_slot,
    emit_armored_envelope,
    mint_password_armor,
    parse_armored_envelope,
    recovery_recipient_public_key,
)
from tools.network.idkit import recovery

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


def _minted():
    key = KeyPair.generate()
    return key, mint_password_armor(key, PASSWORD, iterations=ITERS)


def test_a_factor_policy_identity_is_accepted():
    key, armor = _minted()
    validate(payload(armor, key.public_hex))


def test_a_policy_identity_carrying_a_recovery_slot_is_accepted():
    key, armor = _minted()
    code = recovery.generate_recovery_code()
    with_recovery = emit_armored_envelope(add_recovery_slot(
        parse_armored_envelope(armor),
        root_seed=bytes.fromhex(key.private_hex),
        recovery_recipient_pub=recovery_recipient_public_key(code),
        recovery_pub=recovery.recovery_signing_key(code).public_hex,
        created_at="2026-08-15T00:00:00Z",
    ))
    validate(payload(with_recovery, key.public_hex))


def test_the_retired_multi_lock_format_is_refused():
    key, _ = _minted()
    fake_v2 = (
        "-----BEGIN AUTONOMY NETWORK ROOT KEY-----\n"
        + base64.b64encode(b'{"v": 2}').decode()
        + "\n-----END AUTONOMY NETWORK ROOT KEY-----"
    )
    with pytest.raises(SchemaValidationError):
        validate(payload(fake_v2, key.public_hex))


def test_a_mismatched_root_pub_is_refused():
    key, armor = _minted()
    other = KeyPair.generate()
    with pytest.raises(SchemaValidationError):
        validate(payload(armor, other.public_hex))


def test_plaintext_key_material_is_still_refused():
    key = KeyPair.generate()
    with pytest.raises(SchemaValidationError):
        validate(payload(key.private_hex, key.public_hex))


def test_garbage_is_refused():
    key = KeyPair.generate()
    with pytest.raises(SchemaValidationError):
        validate(payload("not an armor", key.public_hex))
