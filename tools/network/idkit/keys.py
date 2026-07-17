"""Ed25519 key handling.

A key id in idkit is simply the hex encoding of the raw 32-byte Ed25519
public key (64 lowercase hex chars) — unambiguous, self-describing, and
directly usable as a signature-verification key.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .errors import MalformedError, SignatureError

PUBLIC_KEY_HEX_LEN = 64  # 32 raw bytes
SIGNATURE_HEX_LEN = 128  # 64 raw bytes


def _decode_hex(value: str, expected_len: int, what: str) -> bytes:
    if not isinstance(value, str) or len(value) != expected_len or value != value.lower():
        raise MalformedError(f"{what} must be {expected_len} lowercase hex chars")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise MalformedError(f"{what} is not valid hex") from exc


class KeyPair:
    """An Ed25519 keypair. Generate fresh or load from a stored private hex."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._private_key = private_key
        self._public_hex = private_key.public_key().public_bytes_raw().hex()

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_hex(cls, private_hex: str) -> "KeyPair":
        raw = _decode_hex(private_hex, 64, "private key")
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    @property
    def public_hex(self) -> str:
        return self._public_hex

    @property
    def key_id(self) -> str:
        """Key id == hex of the raw public key."""
        return self._public_hex

    @property
    def private_hex(self) -> str:
        return self._private_key.private_bytes_raw().hex()

    def sign(self, data: bytes) -> bytes:
        return self._private_key.sign(data)

    def sign_hex(self, data: bytes) -> str:
        return self.sign(data).hex()


def load_public_key(public_hex: str) -> Ed25519PublicKey:
    raw = _decode_hex(public_hex, PUBLIC_KEY_HEX_LEN, "public key")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise MalformedError("public key bytes are not a valid Ed25519 point") from exc


def verify_signature(public_hex: str, sig_hex: str, data: bytes) -> None:
    """Verify *sig_hex* over *data* against *public_hex*.

    Raises :class:`SignatureError` on mismatch, :class:`MalformedError` on
    undecodable inputs. Returns None on success.
    """
    pub = load_public_key(public_hex)
    sig = _decode_hex(sig_hex, SIGNATURE_HEX_LEN, "signature")
    try:
        pub.verify(sig, data)
    except InvalidSignature as exc:
        raise SignatureError("signature does not verify") from exc
