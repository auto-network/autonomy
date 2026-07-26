"""Sealing primitive: HPKE seal/open round-trip and every fail-closed path."""

from __future__ import annotations

import pytest

from tools.network.idkit.errors import MalformedError
from tools.network.idkit.keys import KeyPair
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
        for bad_suite in (0, 2, 255):
            with pytest.raises(SealingError):
                seal(SECRET, public_hex, PURPOSE, bad_suite)

    def test_seal_rejects_mistyped_suite_id(self, keypair):
        # Not a wire byte at all — a caller type error, not a sealing failure.
        _, public_hex = keypair
        for bad_suite in (-1, 256, 4096, True, None, "1"):
            with pytest.raises(MalformedError):
                seal(SECRET, public_hex, PURPOSE, bad_suite)

    def test_retagged_record_with_known_suite_fails(self, keypair, monkeypatch):
        # The registry check alone would pass a second registered id; the
        # suite id bound into the HPKE info is what must reject the retag.
        from tools.network.idkit import sealing

        private_hex, public_hex = keypair
        monkeypatch.setitem(
            sealing._SUITES, 2, sealing._SUITES[SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305]
        )
        record = seal(SECRET, public_hex, PURPOSE)
        retagged = bytes([2]) + record[1:]
        with pytest.raises(SealingError):
            seal_open(retagged, private_hex, PURPOSE)
        # Sanity: the same bytes under their true id still open.
        assert seal_open(record, private_hex, PURPOSE) == SECRET

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

    def test_degenerate_recipient_key_fails_closed(self):
        # Low-order X25519 points are structurally valid 32-byte strings but
        # have no usable shared secret; recipient keys arrive off the wire,
        # so they must fail in the taxonomy, not as builtins.ValueError.
        low_order = (
            "00" * 32,  # neutral element
            "01" + "00" * 31,  # order-1 point
            "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",  # order-8
        )
        for bad_pub in low_order:
            with pytest.raises(SealingError):
                seal(SECRET, bad_pub, PURPOSE)

    def test_degenerate_enc_point_in_record_fails_closed(self, keypair):
        private_hex, public_hex = keypair
        record = seal(SECRET, public_hex, PURPOSE)
        for bad_enc in (bytes(32), b"\x01" + bytes(31)):
            doctored = record[:1] + bad_enc + record[33:]
            with pytest.raises(SealingError):
                seal_open(doctored, private_hex, PURPOSE)

    def test_signing_key_is_not_an_encapsulation_key(self):
        # HAZARD, pinned deliberately: an Ed25519 signing public key is
        # indistinguishable from an X25519 key as 64 hex chars, seal()
        # accepts it, and the record is PERMANENTLY unopenable — not even
        # the signing-key holder's seed recovers it. The usage-direction
        # rule (seal only to encapsulation keys) is enforced by callers;
        # this test keeps the failure mode visible.
        signer = KeyPair.generate()
        record = seal(SECRET, signer.public_hex, PURPOSE)
        with pytest.raises(SealingError):
            seal_open(record, signer.private_hex, PURPOSE)

    def test_frozen_derivation_vector(self):
        # Known-answer vector, independently computed by the cross-model
        # reviewer (c497640b-da0): locks the HKDF construction and the
        # derivation info label. Also the JS interop anchor (auto-o7g5j).
        private_hex, public_hex = derive_encapsulation_keypair(
            bytes(range(32)), "org-armor.v1"
        )
        assert private_hex == (
            "ed263bb19169879052ba27865490eebe5563ea3e80c23d02c1a899476d398082"
        )
        assert public_hex == (
            "c226869fb52445e3344f86e8f503302b8517e9762912d2947aa54edc6d08296d"
        )

    def test_frozen_sealed_record_opens(self):
        # A pinned wire record sealed to the frozen keypair above: locks the
        # record layout (suite byte || enc || ct) and the seal info label.
        record = bytes.fromhex(
            "0181aa3f6acacbebc6c6fa8ad55c7009817816088242367315eafe7228b288c4"
            "022107b1d55e674f6a700afe5d4cfbb83bfcff4a5ada5990b8ccce6b541fd6c6"
            "a24cb54d834c91b495fad40a9ed78660e9"
        )
        private_hex = "ed263bb19169879052ba27865490eebe5563ea3e80c23d02c1a899476d398082"
        assert seal_open(record, private_hex, "org-armor.v1") == (
            b"the org root armor key material."
        )

    def test_package_exports(self):
        import tools.network.idkit as idkit

        assert idkit.seal is seal
        assert idkit.seal_open is seal_open
        assert idkit.derive_encapsulation_keypair is derive_encapsulation_keypair
        assert idkit.SealingError is SealingError
        assert issubclass(idkit.SealingError, idkit.IdkitError)

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
