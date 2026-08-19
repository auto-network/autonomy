"""Per-organization member recovery key derivation (auto-c3yl1 item 2)."""

from __future__ import annotations

import pytest

from tools.network.idkit.recovery import (
    generate_recovery_code,
    member_recovery_key,
    member_recovery_pub,
    recovery_signing_key,
)
from tools.network.idkit.errors import MalformedError

GEN_A = "1a" * 32
GEN_B = "2b" * 32


def test_deterministic_for_a_code_and_org():
    code = generate_recovery_code()
    assert member_recovery_pub(code, GEN_A) == member_recovery_pub(code, GEN_A)


def test_different_per_organization():
    code = generate_recovery_code()
    assert member_recovery_pub(code, GEN_A) != member_recovery_pub(code, GEN_B)


def test_distinct_from_org_root_recovery_key():
    code = generate_recovery_code()
    assert member_recovery_pub(code, GEN_A) != recovery_signing_key(code).public_hex


def test_different_codes_differ():
    a, b = generate_recovery_code(), generate_recovery_code()
    assert member_recovery_pub(a, GEN_A) != member_recovery_pub(b, GEN_A)


def test_key_can_sign_and_verify():
    from tools.network.idkit import verify_signature
    code = generate_recovery_code()
    kp = member_recovery_key(code, GEN_A)
    sig = kp.sign_hex(b"hello")
    verify_signature(kp.public_hex, sig, b"hello")


def test_rejects_short_code_and_bad_genesis():
    with pytest.raises(MalformedError):
        member_recovery_key(b"too-short", GEN_A)
    with pytest.raises(MalformedError):
        member_recovery_key(generate_recovery_code(), "not-hex")
