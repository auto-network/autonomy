"""Cryptographic suite identifiers for storage key-control records.

One recognized identifier per cryptographic position (contract §11,
revision 2). Every record carries its suite identifiers explicitly, and
negotiation of an unrecognized or withdrawn identifier fails closed at
:func:`require_suite`.

The default body and wrap suite (AES-256-GCM-SIV) is nonce-misuse-
resistant; ChaCha20-Poly1305 covers large one-shot bodies with the whole
ciphertext buffered and the tag verified before any plaintext is
released (verify-then-release).

Reserved chunked body suite — ``BODY_SUITE_CHUNKED_RESERVED`` is a
defined constant excluded from every recognized set, so its negotiation
fails closed until the suite is specified. Fixed requirements for the
suite that will claim the identifier, recorded per contract §11:

- authenticated per-chunk framing with counter-derived nonces (the
  available primitives' 96-bit nonces forbid random per-chunk nonces);
- an authenticated final-chunk marker;
- a decision between the whole-body ciphertext hash and a Merkle chunk
  tree — the latter required if coded-symbol transports are to verify
  stripes before holding the complete body.
"""

from __future__ import annotations

from tools.network.idkit.sealing import SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305

from .errors import SuiteError

HASH_SUITE = "sha-256"
SIGNATURE_SUITE = "ed25519"

#: Capability delivery: the idkit sealing version-one identifier — hybrid
#: public-key encryption (RFC 9180 base mode) over X25519 / HKDF-SHA-256 /
#: ChaCha20-Poly1305. Same value the sealed wire record is tagged with.
SEAL_SUITE = SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305

WRAP_SUITE = "aes-256-gcm-siv"
BODY_SUITE_DEFAULT = "aes-256-gcm-siv"
#: Large one-shot bodies: buffered, tag verified before plaintext release.
BODY_SUITE_LARGE = "chacha20-poly1305"

#: Reserved — recognized by no position; see the module docstring.
BODY_SUITE_CHUNKED_RESERVED = "chunked-aead-v1"

HASH_SUITES = frozenset({HASH_SUITE})
SIGNATURE_SUITES = frozenset({SIGNATURE_SUITE})
SEAL_SUITES = frozenset({SEAL_SUITE})
WRAP_SUITES = frozenset({WRAP_SUITE})
BODY_SUITES = frozenset({BODY_SUITE_DEFAULT, BODY_SUITE_LARGE})


def require_suite(suite_id, recognized) -> None:
    """Fail closed unless *suite_id* is in the *recognized* set."""
    if suite_id not in recognized:
        raise SuiteError(f"unrecognized suite identifier: {suite_id!r}")
