"""Weak-S2K rejection floor for provisioned signing-key ciphertext (D3-21).

DN3 §6 (graph note a498f525-6b0): the floor is evaluated by **parsing the
actual OpenPGP String-to-Key (S2K) specifier bytes inside the encrypted key
ciphertext** — never a declared/sidecar ``kdf_params`` field the credential
store might keep alongside the blob. A sidecar field is a claim the
credential store's ingestion path could get wrong or have lied to; only the
packet itself is authoritative.

**Byte contract.** This module parses a caller-supplied byte string shaped as
``cipher_algo_byte + s2k_specifier_bytes`` — the two fields RFC 4880 §5.5.3
places immediately after the string-to-key usage octet in a Secret-Key
packet, in the order they appear on the wire. Locating and slicing this
substring out of a full OpenPGP Secret-Key packet (which also carries public
key material, an optional IV, and the encrypted key material itself) is the
caller's concern — the credential store is what actually holds the raw
packet bytes and knows its own on-disk layout; this module owns only the
S2K-specifier decode and the floor policy, per RFC 4880 §3.7.1 plus GnuPG's
Argon2 extension (``--s2k-argon2``, S2K type 4 per the OpenPGP
crypto-refresh draft: 16-byte salt, then 1-byte iterations (t), 1-byte
parallelism (p), 1-byte log2(memory-in-KiB) (m)).
"""

from __future__ import annotations

from dataclasses import dataclass

# RFC 4880 §9.2 symmetric-key algorithm IDs (the ones this floor cares about).
_CIPHER_NAMES = {
    1: "IDEA",
    2: "3DES",
    3: "CAST5",
    4: "Blowfish",
    7: "AES128",
    8: "AES192",
    9: "AES256",
    10: "Twofish",
}
_AES256_CIPHER_ID = 9
_WEAK_CIPHER_IDS = {1, 2, 3, 4, 7, 8, 10}  # everything weaker than AES-256

# RFC 4880 §9.4 hash algorithm IDs.
_HASH_NAMES = {1: "MD5", 2: "SHA-1", 8: "SHA-256", 10: "SHA-512"}
_WEAK_HASH_IDS = {1, 2}  # MD5, SHA-1
_STRONG_HASH_IDS = {8, 10}  # SHA-256, SHA-512

S2K_SIMPLE = 0
S2K_SALTED = 1
S2K_ITERATED_SALTED = 3
S2K_ARGON2 = 4  # GnuPG >=2.3 --s2k-argon2

ITERATION_COUNT_FLOOR = 4_194_304  # 2^22
ARGON2_MIN_MEMORY_KIB = 65_536  # 64 MiB
ARGON2_MIN_ITERATIONS = 3
ARGON2_MIN_PARALLELISM = 1


class MalformedS2KPacket(ValueError):
    pass


@dataclass(frozen=True)
class S2KParams:
    s2k_type: int
    cipher_id: int
    hash_id: int | None = None
    iteration_count: int | None = None
    argon2_memory_kib: int | None = None
    argon2_iterations: int | None = None
    argon2_parallelism: int | None = None

    @property
    def algorithm(self) -> str:
        if self.s2k_type == S2K_ARGON2:
            return "argon2id"
        return "s2k"

    def to_kdf_params(self) -> dict:
        if self.s2k_type == S2K_ARGON2:
            return {
                "algorithm": "argon2id",
                "argon2_memory_kib": self.argon2_memory_kib,
                "argon2_iterations": self.argon2_iterations,
                "argon2_parallelism": self.argon2_parallelism,
            }
        return {
            "algorithm": "s2k",
            "s2k_type": self.s2k_type,
            "cipher": _CIPHER_NAMES.get(self.cipher_id, f"unknown-{self.cipher_id}"),
            "hash": _HASH_NAMES.get(self.hash_id, f"unknown-{self.hash_id}") if self.hash_id is not None else None,
            "s2k_count": self.iteration_count,
        }


def _decode_s2k_count(coded: int) -> int:
    """RFC 4880 §3.7.1.3 — the coded one-octet iteration count."""
    return (16 + (coded & 15)) << ((coded >> 4) + 6)


def parse_s2k_packet(data: bytes) -> S2KParams:
    """Parse ``cipher_algo_byte + s2k_specifier_bytes``. Raises
    :class:`MalformedS2KPacket` on truncated or unrecognized input — a
    parse failure is a rejection, never a silent accept."""
    if len(data) < 2:
        raise MalformedS2KPacket("packet too short for cipher algo + S2K type")
    cipher_id = data[0]
    s2k_type = data[1]
    rest = data[2:]

    if s2k_type == S2K_SIMPLE:
        if len(rest) < 1:
            raise MalformedS2KPacket("truncated Simple S2K (missing hash algo)")
        return S2KParams(s2k_type=s2k_type, cipher_id=cipher_id, hash_id=rest[0])

    if s2k_type == S2K_SALTED:
        if len(rest) < 1 + 8:
            raise MalformedS2KPacket("truncated Salted S2K (missing hash algo/salt)")
        return S2KParams(s2k_type=s2k_type, cipher_id=cipher_id, hash_id=rest[0])

    if s2k_type == S2K_ITERATED_SALTED:
        if len(rest) < 1 + 8 + 1:
            raise MalformedS2KPacket("truncated Iterated+Salted S2K")
        hash_id = rest[0]
        coded_count = rest[9]
        return S2KParams(
            s2k_type=s2k_type, cipher_id=cipher_id, hash_id=hash_id,
            iteration_count=_decode_s2k_count(coded_count),
        )

    if s2k_type == S2K_ARGON2:
        if len(rest) < 16 + 1 + 1 + 1:
            raise MalformedS2KPacket("truncated Argon2 S2K")
        t, p, log2_m = rest[16], rest[17], rest[18]
        return S2KParams(
            s2k_type=s2k_type, cipher_id=cipher_id,
            argon2_memory_kib=1 << log2_m,
            argon2_iterations=t,
            argon2_parallelism=p,
        )

    raise MalformedS2KPacket(f"unrecognized S2K type: {s2k_type}")


def evaluate_floor(params: S2KParams) -> list[str]:
    """Return the list of floor violations — empty means the blob is
    accepted. Never partial-credit: any violation rejects the whole blob."""
    violations: list[str] = []

    if params.s2k_type == S2K_ARGON2:
        if (params.argon2_memory_kib or 0) < ARGON2_MIN_MEMORY_KIB:
            violations.append(
                f"argon2 memory {params.argon2_memory_kib} KiB below floor {ARGON2_MIN_MEMORY_KIB} KiB"
            )
        if (params.argon2_iterations or 0) < ARGON2_MIN_ITERATIONS:
            violations.append(
                f"argon2 iterations {params.argon2_iterations} below floor {ARGON2_MIN_ITERATIONS}"
            )
        if (params.argon2_parallelism or 0) < ARGON2_MIN_PARALLELISM:
            violations.append(
                f"argon2 parallelism {params.argon2_parallelism} below floor {ARGON2_MIN_PARALLELISM}"
            )
        return violations

    if params.s2k_type in (S2K_SIMPLE, S2K_SALTED):
        violations.append(
            f"S2K type {params.s2k_type} is non-iterated (trivially brute-forceable offline)"
        )
        return violations

    if params.s2k_type != S2K_ITERATED_SALTED:
        violations.append(f"unrecognized S2K type: {params.s2k_type}")
        return violations

    if params.hash_id in _WEAK_HASH_IDS:
        violations.append(f"hash {_HASH_NAMES.get(params.hash_id, params.hash_id)} is weak")
    elif params.hash_id not in _STRONG_HASH_IDS:
        violations.append(f"hash id {params.hash_id} is not an accepted strong hash")

    if params.cipher_id in _WEAK_CIPHER_IDS:
        violations.append(f"cipher {_CIPHER_NAMES.get(params.cipher_id, params.cipher_id)} is weaker than AES-256")
    elif params.cipher_id != _AES256_CIPHER_ID:
        violations.append(f"cipher id {params.cipher_id} is not AES-256")

    if (params.iteration_count or 0) < ITERATION_COUNT_FLOOR:
        violations.append(
            f"iteration count {params.iteration_count} below floor {ITERATION_COUNT_FLOOR}"
        )

    return violations
