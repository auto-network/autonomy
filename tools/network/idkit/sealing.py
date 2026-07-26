"""Hybrid public-key sealing: encrypt a small secret to a recipient key.

The shared primitive behind every "deliver key material to a holder of a
private key" operation — root-key armor to an owner, content-key and
storage-state material to members, re-delivered material after a
credential change. Hybrid public-key encryption (RFC 9180, base mode) as
provided by ``cryptography`` — no additional dependency: key
encapsulation over X25519, key derivation over HKDF-SHA-256,
authenticated encryption with ChaCha20-Poly1305.

Wire record::

    suite_id (1 byte) || enc (KEM encapsulated key) || ciphertext

The suite identifier is validated on open and an unrecognized value
fails closed. The *record format* admits a future suite by registering a
new identifier — no structural change to the wire layout — but the code
paths here (64-hex keys, ``from_public_bytes``) are X25519-specific and
would need their own handling for a suite with different key or enc
sizes. The ``purpose`` label is bound into the HPKE encryption context
alongside the suite identifier, so a record sealed for one purpose (or
re-tagged with a different known suite) never opens under another.

WARNING — usage direction: the recipient key MUST be an X25519
encapsulation public key, never an Ed25519 signing key. Both render as
64 lowercase hex and no structural check can tell them apart, but a
record sealed to a signing key is PERMANENTLY UNOPENABLE — nobody holds
the matching X25519 private key. Callers that must mint an encapsulation
keypair use :func:`derive_encapsulation_keypair`, which derives an
independent X25519 keypair from a seed under a purpose label via
HKDF-SHA-256; the signing key is never reinterpreted as an encapsulation
key, in either direction.

Multi-recipient delivery is one :func:`seal` per recipient public key —
no shared secret, no helper here.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, hpke
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import MalformedError, SealingError
from .keys import _decode_hex

__all__ = [
    "SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305",
    "SealingError",
    "derive_encapsulation_keypair",
    "open",
    "seal",
]

#: Version-one suite: X25519 / HKDF-SHA-256 / ChaCha20-Poly1305.
SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305 = 1

_SUITES = {
    SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305: hpke.Suite(
        hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305
    ),
}

_SEAL_INFO_PREFIX = "autonomy.idkit.seal.v"
_DERIVE_INFO_PREFIX = b"autonomy.idkit.encap-key.v1\n"

_MIN_SEED_LEN = 32


def _validate_purpose(purpose: str) -> bytes:
    if (
        not isinstance(purpose, str)
        or not purpose
        or not purpose.isascii()
        or not purpose.isprintable()
    ):
        raise MalformedError("purpose must be a non-empty printable ASCII label")
    return purpose.encode("ascii")


def _seal_info(suite_id: int, purpose: bytes) -> bytes:
    # Binds both the suite identifier and the purpose into the HPKE
    # encryption context: re-tagging a record with another (known) suite
    # id, or presenting it for a different purpose, fails authentication.
    return f"{_SEAL_INFO_PREFIX}{suite_id}\n".encode("ascii") + purpose


def _lookup_suite(suite_id: int) -> hpke.Suite:
    suite = _SUITES.get(suite_id)
    if suite is None:
        raise SealingError(f"unrecognized sealing suite identifier: {suite_id!r}")
    return suite


def seal(
    plaintext: bytes,
    recipient_public_key: str,
    purpose: str,
    suite_id: int = SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305,
) -> bytes:
    """Encrypt *plaintext* to the recipient's encapsulation public key.

    *recipient_public_key* is the 64-lowercase-hex raw X25519 public key
    (as produced by :func:`derive_encapsulation_keypair`) — NEVER an
    Ed25519 signing key: the two are indistinguishable as hex, and a
    record sealed to a signing key is permanently unopenable. Returns the
    suite-tagged wire record.
    """
    if not isinstance(plaintext, (bytes, bytearray)):
        raise MalformedError("plaintext must be bytes")
    label = _validate_purpose(purpose)
    if not isinstance(suite_id, int) or isinstance(suite_id, bool) or not 0 <= suite_id <= 255:
        raise MalformedError("suite_id must be an int in [0, 255]")
    suite = _lookup_suite(suite_id)
    raw = _decode_hex(recipient_public_key, 64, "recipient public key")
    try:
        pub = X25519PublicKey.from_public_bytes(raw)
        sealed = suite.encrypt(bytes(plaintext), pub, info=_seal_info(suite_id, label))
    except ValueError as exc:
        # Low-order / degenerate points pass from_public_bytes and fail in
        # the KEM; recipient keys arrive off the wire, so fail closed in
        # the taxonomy instead of leaking builtins.ValueError.
        raise SealingError("recipient public key cannot be sealed to") from exc
    return bytes([suite_id]) + sealed


def open(record: bytes, recipient_private_key: str, purpose: str) -> bytes:  # noqa: A001
    """Open a sealed *record* with the recipient's private key and *purpose*.

    Raises :class:`SealingError` for a mismatched purpose, a wrong key, a
    tampered record, or an unknown suite identifier.
    """
    if not isinstance(record, (bytes, bytearray)):
        raise MalformedError("record must be bytes")
    label = _validate_purpose(purpose)
    raw = _decode_hex(recipient_private_key, 64, "recipient private key")
    try:
        priv = X25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise MalformedError("recipient private key is not a valid X25519 key") from exc
    if len(record) < 1:
        raise SealingError("sealed record is empty")
    suite_id = record[0]
    suite = _lookup_suite(suite_id)
    try:
        return suite.decrypt(bytes(record[1:]), priv, info=_seal_info(suite_id, label))
    except (InvalidTag, ValueError) as exc:
        # ValueError guards degenerate attacker-controlled enc points, which
        # must be indistinguishable from any other failed open.
        raise SealingError("sealed record does not open with that key and purpose") from exc


def derive_encapsulation_keypair(seed: bytes, purpose: str) -> tuple[str, str]:
    """Derive an independent X25519 keypair from *seed* under *purpose*.

    HKDF-SHA-256 with a purpose-labelled info string, so keypairs for
    distinct purposes are distinct and non-interchangeable, and the seed
    (even a signing-key seed) is never reinterpreted as an encapsulation
    key. Deterministic for a given seed and purpose. Returns
    ``(private_hex, public_hex)``, both 64 lowercase hex chars.
    """
    if not isinstance(seed, (bytes, bytearray)):
        raise MalformedError("seed must be bytes")
    if len(seed) < _MIN_SEED_LEN:
        raise MalformedError(f"seed must be at least {_MIN_SEED_LEN} bytes")
    label = _validate_purpose(purpose)
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_DERIVE_INFO_PREFIX + label,
    ).derive(bytes(seed))
    priv = X25519PrivateKey.from_private_bytes(raw)
    return raw.hex(), priv.public_key().public_bytes_raw().hex()
