"""Sealing primitive: HPKE seal/open round-trip and every fail-closed path."""

from __future__ import annotations

import pytest

from tools.network.idkit.errors import MalformedError
from tools.network.idkit.sealing import (
    SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305,
    SealingError,
    derive_encapsulation_keypair,
    open as seal_open,
    seal,
)

SEED = bytes(range(32))
OTHER_SEED = bytes(range(1, 33))
PURPOSE = "org-armor.v1"
OTHER_PURPOSE = "content-key.v1"
SECRET = b"\x00\x01" + b"s" * 30  # a 32-byte "root key" style payload


@pytest.fixture
def keypair():
    return derive_encapsulation_keypair(SEED, PURPOSE)


class TestSealing:
    def test_roundtrip_recovers_plaintext(self, keypair):
        private_hex, public_hex = keypair
        record = seal(SECRET, public_hex, PURPOSE)
        assert seal_open(record, private_hex, PURPOSE) == SECRET

    def test_roundtrip_empty_and_large_plaintexts(self, keypair):
        private_hex, public_hex = keypair
        for plaintext in (b"", b"x", b"p" * 4096):
            record = seal(plaintext, public_hex, PURPOSE)
            assert seal_open(record, private_hex, PURPOSE) == plaintext

    def test_record_is_suite_tagged(self, keypair):
        _, public_hex = keypair
        record = seal(SECRET, public_hex, PURPOSE)
        assert record[0] == SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305
        # 1-byte suite id + 32-byte X25519 enc + ciphertext + 16-byte tag
        assert len(record) == 1 + 32 + len(SECRET) + 16

    def test_sealing_is_randomized(self, keypair):
        _, public_hex = keypair
        assert seal(SECRET, public_hex, PURPOSE) != seal(SECRET, public_hex, PURPOSE)

    def test_wrong_private_key_does_not_open(self, keypair):
        _, public_hex = keypair
        other_private, _ = derive_encapsulation_keypair(OTHER_SEED, PURPOSE)
        record = seal(SECRET, public_hex, PURPOSE)
        with pytest.raises(SealingError):
            seal_open(record, other_private, PURPOSE)

    def test_wrong_purpose_does_not_open(self, keypair):
        private_hex, public_hex = keypair
        record = seal(SECRET, public_hex, PURPOSE)
        with pytest.raises(SealingError):
            seal_open(record, private_hex, OTHER_PURPOSE)

    def test_unrecognized_suite_fails_closed(self, keypair):
        private_hex, public_hex = keypair
        record = seal(SECRET, public_hex, PURPOSE)
        for bad_suite in (0, 2, 255):
            retagged = bytes([bad_suite]) + record[1:]
            with pytest.raises(SealingError):
                seal_open(retagged, private_hex, PURPOSE)

    def test_seal_rejects_unrecognized_suite(self, keypair):
        _, public_hex = keypair
        for bad_suite in (0, 2, 255, -1, 4096, True, None, "1"):
            with pytest.raises(SealingError):
                seal(SECRET, public_hex, PURPOSE, bad_suite)

    def test_tampered_record_does_not_open(self, keypair):
        private_hex, public_hex = keypair
        record = seal(SECRET, public_hex, PURPOSE)
        # Flip one bit in the enc (offset 1), the ciphertext body, and the tag.
        for offset in (1, 1 + 32, len(record) - 1):
            tampered = bytearray(record)
            tampered[offset] ^= 0x01
            with pytest.raises(SealingError):
                seal_open(bytes(tampered), private_hex, PURPOSE)

    def test_truncated_record_does_not_open(self, keypair):
        private_hex, public_hex = keypair
        record = seal(SECRET, public_hex, PURPOSE)
        for cut in (1, 32, len(record) - 1):
            with pytest.raises(SealingError):
                seal_open(record[:cut], private_hex, PURPOSE)
        with pytest.raises(SealingError):
            seal_open(b"", private_hex, PURPOSE)

    def test_derivation_is_deterministic(self):
        assert derive_encapsulation_keypair(SEED, PURPOSE) == derive_encapsulation_keypair(
            SEED, PURPOSE
        )

    def test_derivation_shape(self, keypair):
        private_hex, public_hex = keypair
        for value in (private_hex, public_hex):
            assert len(value) == 64
            assert value == value.lower()
            bytes.fromhex(value)

    def test_distinct_purposes_yield_distinct_keypairs(self):
        pair_a = derive_encapsulation_keypair(SEED, PURPOSE)
        pair_b = derive_encapsulation_keypair(SEED, OTHER_PURPOSE)
        assert pair_a[0] != pair_b[0]
        assert pair_a[1] != pair_b[1]

    def test_distinct_seeds_yield_distinct_keypairs(self):
        assert derive_encapsulation_keypair(SEED, PURPOSE) != derive_encapsulation_keypair(
            OTHER_SEED, PURPOSE
        )

    def test_purpose_derived_keys_are_not_interchangeable(self):
        # Sealed to the purpose-A keypair: neither the purpose-B private key
        # (wrong key) nor the A key under purpose B (wrong context) opens it.
        private_a, public_a = derive_encapsulation_keypair(SEED, PURPOSE)
        private_b, _ = derive_encapsulation_keypair(SEED, OTHER_PURPOSE)
        record = seal(SECRET, public_a, PURPOSE)
        with pytest.raises(SealingError):
            seal_open(record, private_b, PURPOSE)
        with pytest.raises(SealingError):
            seal_open(record, private_b, OTHER_PURPOSE)
        with pytest.raises(SealingError):
            seal_open(record, private_a, OTHER_PURPOSE)

    @pytest.mark.parametrize("bad_purpose", ["", "läbel", "a\nb", "tab\tlabel", None, 42])
    def test_malformed_purpose_is_rejected(self, keypair, bad_purpose):
        private_hex, public_hex = keypair
        with pytest.raises(MalformedError):
            seal(SECRET, public_hex, bad_purpose)
        with pytest.raises(MalformedError):
            seal_open(b"\x01" + b"x" * 48, private_hex, bad_purpose)
        with pytest.raises(MalformedError):
            derive_encapsulation_keypair(SEED, bad_purpose)

    @pytest.mark.parametrize("bad_key", ["", "ab" * 31, "AB" * 32, "zz" * 32, None, 1234])
    def test_malformed_keys_are_rejected(self, keypair, bad_key):
        private_hex, public_hex = keypair
        with pytest.raises(MalformedError):
            seal(SECRET, bad_key, PURPOSE)
        with pytest.raises(MalformedError):
            seal_open(seal(SECRET, public_hex, PURPOSE), bad_key, PURPOSE)

    def test_malformed_plaintext_record_and_seed_are_rejected(self, keypair):
        private_hex, public_hex = keypair
        with pytest.raises(MalformedError):
            seal("not bytes", public_hex, PURPOSE)
        with pytest.raises(MalformedError):
            seal_open("not bytes", private_hex, PURPOSE)
        with pytest.raises(MalformedError):
            derive_encapsulation_keypair("not bytes", PURPOSE)
        with pytest.raises(MalformedError):
            derive_encapsulation_keypair(SEED[:16], PURPOSE)
