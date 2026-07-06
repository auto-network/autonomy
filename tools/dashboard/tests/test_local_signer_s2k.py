"""Tests for the weak-S2K floor (D3-21).

No real GnuPG-generated blob is available in this environment (only
``gpgv``, verify-only, is in the agent image; the dashboard image has
neither). Round-trips are verified against a local encoder that constructs
packets per RFC 4880 §3.7.1 byte-for-byte, so the parser is checked against
the spec directly rather than against one library's output.
"""

from __future__ import annotations

import struct

import pytest

from tools.dashboard.local_signer_s2k import (
    MalformedS2KPacket,
    S2K_ARGON2,
    S2K_ITERATED_SALTED,
    S2K_SALTED,
    S2K_SIMPLE,
    evaluate_floor,
    parse_s2k_packet,
)

AES256 = 9
AES128 = 7
CAST5 = 3
SHA256 = 8
SHA512 = 10
SHA1 = 2
MD5 = 1


def _encode_coded_count(count: int) -> int:
    """Inverse of the RFC 4880 §3.7.1.3 decode formula, for constructing
    a packet with a specific decoded iteration count in tests."""
    for exponent in range(0, 256):
        for base in range(16):
            candidate = (16 + base) << exponent
            if candidate == count:
                return base | ((exponent - 6) << 4)
    raise ValueError(f"{count} is not exactly representable by the coded S2K count")


def _iterated_salted_packet(*, cipher_id: int, hash_id: int, count: int) -> bytes:
    return bytes([cipher_id, S2K_ITERATED_SALTED, hash_id]) + b"\x00" * 8 + bytes([_encode_coded_count(count)])


def _simple_packet(*, cipher_id: int, hash_id: int) -> bytes:
    return bytes([cipher_id, S2K_SIMPLE, hash_id])


def _salted_packet(*, cipher_id: int, hash_id: int) -> bytes:
    return bytes([cipher_id, S2K_SALTED, hash_id]) + b"\x00" * 8


def _argon2_packet(*, cipher_id: int, memory_kib: int, iterations: int, parallelism: int) -> bytes:
    log2_m = memory_kib.bit_length() - 1
    assert 1 << log2_m == memory_kib, "test helper requires an exact power of two"
    return bytes([cipher_id, S2K_ARGON2]) + b"\x00" * 16 + bytes([iterations, parallelism, log2_m])


# ── Parsing ──────────────────────────────────────────────────────────


def test_parse_iterated_salted_round_trip():
    packet = _iterated_salted_packet(cipher_id=AES256, hash_id=SHA512, count=4_194_304)
    params = parse_s2k_packet(packet)
    assert params.s2k_type == S2K_ITERATED_SALTED
    assert params.cipher_id == AES256
    assert params.hash_id == SHA512
    assert params.iteration_count == 4_194_304


def test_parse_argon2_round_trip():
    packet = _argon2_packet(cipher_id=AES256, memory_kib=65_536, iterations=3, parallelism=1)
    params = parse_s2k_packet(packet)
    assert params.s2k_type == S2K_ARGON2
    assert params.argon2_memory_kib == 65_536
    assert params.argon2_iterations == 3
    assert params.argon2_parallelism == 1


def test_parse_truncated_packet_raises():
    with pytest.raises(MalformedS2KPacket):
        parse_s2k_packet(bytes([AES256, S2K_ITERATED_SALTED, SHA512]))  # missing salt+count


def test_parse_unrecognized_s2k_type_raises():
    with pytest.raises(MalformedS2KPacket):
        parse_s2k_packet(bytes([AES256, 99]))


# ── Floor: reject classes ────────────────────────────────────────────


def test_reject_simple_s2k():
    params = parse_s2k_packet(_simple_packet(cipher_id=AES256, hash_id=SHA512))
    assert evaluate_floor(params)


def test_reject_salted_s2k():
    params = parse_s2k_packet(_salted_packet(cipher_id=AES256, hash_id=SHA512))
    assert evaluate_floor(params)


def test_reject_iterated_salted_with_md5_or_sha1():
    for weak_hash in (MD5, SHA1):
        params = parse_s2k_packet(
            _iterated_salted_packet(cipher_id=AES256, hash_id=weak_hash, count=8_388_608)
        )
        assert evaluate_floor(params), f"hash {weak_hash} should be rejected"


def test_reject_cipher_weaker_than_aes256():
    for weak_cipher in (CAST5, AES128):
        params = parse_s2k_packet(
            _iterated_salted_packet(cipher_id=weak_cipher, hash_id=SHA512, count=8_388_608)
        )
        assert evaluate_floor(params), f"cipher {weak_cipher} should be rejected"


def test_reject_count_below_floor():
    params = parse_s2k_packet(
        _iterated_salted_packet(cipher_id=AES256, hash_id=SHA512, count=2_097_152)  # 2^21, half the floor
    )
    assert evaluate_floor(params)


def test_reject_argon2_below_memory_floor():
    params = parse_s2k_packet(
        _argon2_packet(cipher_id=AES256, memory_kib=32_768, iterations=3, parallelism=1)
    )
    assert evaluate_floor(params)


def test_reject_argon2_below_iteration_floor():
    params = parse_s2k_packet(
        _argon2_packet(cipher_id=AES256, memory_kib=65_536, iterations=1, parallelism=1)
    )
    assert evaluate_floor(params)


# ── Floor: accept classes (positive controls) ────────────────────────


def test_accept_iterated_salted_at_floor_exactly():
    """At-floor, not above — proves the check enforces a floor rather
    than silently rejecting everything."""
    params = parse_s2k_packet(
        _iterated_salted_packet(cipher_id=AES256, hash_id=SHA512, count=4_194_304)
    )
    assert evaluate_floor(params) == []


def test_accept_iterated_salted_sha256():
    params = parse_s2k_packet(
        _iterated_salted_packet(cipher_id=AES256, hash_id=SHA256, count=4_194_304)
    )
    assert evaluate_floor(params) == []


def test_accept_argon2_at_floor_exactly():
    params = parse_s2k_packet(
        _argon2_packet(cipher_id=AES256, memory_kib=65_536, iterations=3, parallelism=1)
    )
    assert evaluate_floor(params) == []


# ── Sidecar cannot lie (the actual security property) ────────────────


def test_sidecar_claim_ignored_packet_is_authoritative():
    """A blob whose sidecar metadata claims strong params while the actual
    packet is weak must be REJECTED — proves the floor parses the real
    packet and does not trust a declared value alongside it."""
    weak_packet = _simple_packet(cipher_id=CAST5, hash_id=SHA1)
    sidecar_claim = {"algorithm": "argon2id", "argon2_memory_kib": 65536, "argon2_iterations": 3}

    # The floor check only ever looks at the parsed packet — the sidecar
    # claim is not even passed to evaluate_floor. This test documents and
    # locks in that the caller must not substitute the sidecar for it.
    params = parse_s2k_packet(weak_packet)
    assert evaluate_floor(params), "packet is weak regardless of what the sidecar claims"
    assert params.to_kdf_params() != sidecar_claim
