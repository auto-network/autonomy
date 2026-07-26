"""Ed25519 keypair basics: generation, signing, storage round-trip."""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair, MalformedError, SignatureError, verify_signature


def test_generate_produces_hex_identifiers():
    kp = KeyPair.generate()
    assert len(kp.public_hex) == 64
    assert kp.public_hex == kp.public_hex.lower()
    bytes.fromhex(kp.public_hex)
    assert kp.key_id == kp.public_hex


def test_generate_is_not_deterministic():
    assert KeyPair.generate().public_hex != KeyPair.generate().public_hex


def test_sign_and_verify_roundtrip():
    kp = KeyPair.generate()
    sig = kp.sign_hex(b"payload")
    assert len(sig) == 128
    verify_signature(kp.public_hex, sig, b"payload")


def test_verify_rejects_wrong_key():
    kp, other = KeyPair.generate(), KeyPair.generate()
    sig = kp.sign_hex(b"payload")
    with pytest.raises(SignatureError):
        verify_signature(other.public_hex, sig, b"payload")


def test_verify_rejects_tampered_data():
    kp = KeyPair.generate()
    sig = kp.sign_hex(b"payload")
    with pytest.raises(SignatureError):
        verify_signature(kp.public_hex, sig, b"payloae")


def test_private_hex_roundtrip_restores_identity():
    kp = KeyPair.generate()
    restored = KeyPair.from_private_hex(kp.private_hex)
    assert restored.public_hex == kp.public_hex
    # Ed25519 signing is deterministic: same key + payload -> same signature.
    assert restored.sign_hex(b"data") == kp.sign_hex(b"data")


@pytest.mark.parametrize(
    "bad_pub",
    [
        "",  # empty
        "ab" * 31,  # too short
        "ab" * 33,  # too long
        "AB" * 32,  # uppercase
        "zz" * 32,  # not hex
        "  " + "ab" * 31,  # right length, but bytes.fromhex would skip whitespace
        "ab" * 31 + "\n\t",  # trailing whitespace
        None,
        1234,
    ],
)
def test_verify_rejects_malformed_public_key(bad_pub):
    kp = KeyPair.generate()
    sig = kp.sign_hex(b"payload")
    with pytest.raises(MalformedError):
        verify_signature(bad_pub, sig, b"payload")


@pytest.mark.parametrize("bad_sig", ["", "ab" * 63, "AB" * 64, "zz" * 64, None])
def test_verify_rejects_malformed_signature(bad_sig):
    kp = KeyPair.generate()
    with pytest.raises(MalformedError):
        verify_signature(kp.public_hex, bad_sig, b"payload")


def test_from_private_hex_rejects_malformed_input():
    with pytest.raises(MalformedError):
        KeyPair.from_private_hex("nope")
