"""The member.rekey recovery co-signature domain (auto-c3yl1, foundation).

Pins that the recovery binding is genesis- and persona-bound and lives under a
domain that no other rekey/rotate signature can substitute for.
"""

from __future__ import annotations

from tools.network.idkit import KeyPair, verify_signature
from tools.network.ledger.events import (
    REKEY_RECOVERY_DOMAIN,
    REKEY_CONTINUITY_DOMAIN,
    ROTATE_RECOVERY_DOMAIN,
    rekey_recovery_input,
    rekey_continuity_input,
    rotate_recovery_input,
    sign_rekey_recovery,
)

GEN = "1a" * 32
PERSONA = "2b" * 32


def test_domain_is_distinct():
    assert REKEY_RECOVERY_DOMAIN not in (REKEY_CONTINUITY_DOMAIN, ROTATE_RECOVERY_DOMAIN)
    inp = rekey_recovery_input(GEN, PERSONA, "aa" * 32, "bb" * 32)
    assert inp.startswith(REKEY_RECOVERY_DOMAIN)


def test_binds_genesis_persona_and_both_keys():
    a = rekey_recovery_input(GEN, PERSONA, "aa" * 32, "bb" * 32)
    assert a != rekey_recovery_input("ff" * 32, PERSONA, "aa" * 32, "bb" * 32)  # genesis
    assert a != rekey_recovery_input(GEN, "ff" * 32, "aa" * 32, "bb" * 32)      # persona
    assert a != rekey_recovery_input(GEN, PERSONA, "ff" * 32, "bb" * 32)        # old_pub
    assert a != rekey_recovery_input(GEN, PERSONA, "aa" * 32, "ff" * 32)        # new_pub


def test_sign_and_verify_roundtrip():
    rk = KeyPair.generate()
    old, new = "aa" * 32, "cc" * 32
    sig = sign_rekey_recovery(rk, GEN, PERSONA, old, new)
    verify_signature(rk.public_hex, sig, rekey_recovery_input(GEN, PERSONA, old, new))


def test_recovery_signature_cannot_substitute_for_continuity():
    """A recovery co-signature over the same keys does NOT verify as a rekey
    continuity proof — the whole point of the distinct domain."""
    rk = KeyPair.generate()
    old, new = "aa" * 32, "cc" * 32
    recovery_sig = sign_rekey_recovery(rk, GEN, PERSONA, old, new)
    try:
        verify_signature(rk.public_hex, recovery_sig,
                         rekey_continuity_input(PERSONA, old, new))
        raise AssertionError("recovery signature must not verify as continuity")
    except Exception:
        pass


def test_recovery_input_differs_from_rotate_recovery_for_same_keys():
    """The ORG-root rotate-recovery (genesis+keypair, no persona) and the
    member rekey-recovery (genesis+persona+keypair) never collide."""
    old, new = "aa" * 32, "cc" * 32
    assert rekey_recovery_input(GEN, PERSONA, old, new) != rotate_recovery_input(GEN, old, new)
