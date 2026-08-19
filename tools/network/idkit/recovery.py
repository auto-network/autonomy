"""The recovery code's cold, dual-function seed — the Python counterpart.

The browser ceremony (``ceremony/recovery.js``) is where a recovery code is
minted, because the code must never touch a server. This module is its exact
mirror, for the paths the browser cannot serve: the command line, the tests,
and opening an armor from a recovery code when there is no browser to open it
in. Both sides must agree byte for byte or a code printed by one would not
open an identity locked by the other, which is the one failure a recovery
mechanism may never have.

One code yields BOTH recovery functions, kept DOMAIN-SEPARATED by distinct
HKDF info strings: an Ed25519 recovery keypair (signing -- co-signs an org
root ``key.rotate``, the ``recovery_pub`` genesis declares) and a KEK-recovery
seed (decryption -- reconstructs the key that unwraps the personal armor).
Neither can be reconstructed from the other.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from .errors import MalformedError
from .keys import KeyPair

#: Distinct info per function -- these strings ARE the domain separation, and
#: neither collides with the persona/KEM derivations.
RECOVERY_SIGN_INFO = "autonomy.recovery.signing.v1"
RECOVERY_KEK_INFO = "autonomy.recovery.kek.v1"
#: A MEMBER's per-organization recovery signing key (auto-c3yl1). Distinct from
#: RECOVERY_SIGN_INFO (the org-root recovery key the genesis declares) AND folded
#: with the genesis id, so a member's recovery key is per-organization: the same
#: recovery code yields a DIFFERENT key in every org, so an identical value never
#: links a member's personas across organizations (the same unlinkability the
#: persona derivation gives, one level down on the recovery axis).
MEMBER_RECOVERY_SIGN_INFO = "autonomy.recovery.member-sign.v1"
RECOVERY_MIN_CODE_BYTES = 32  # at least 256 bits of entropy
_GENESIS_ID_HEX_LEN = 64

#: Crockford base32: no I, L, O or U, so 1/I/L and 0/O cannot be misread apart
#: and the decoder maps the confusable glyphs home.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _hkdf32(ikm: bytes, info: str) -> bytes:
    """HKDF-SHA256 to 32 bytes, salt=None convention (all-zero digest-sized).

    Matches the WebCrypto call in ``recovery.js`` exactly: the info string
    carries the domain, the salt is a zero block.
    """
    prk = hmac.new(bytes(32), ikm, hashlib.sha256).digest()
    return hmac.new(prk, info.encode("ascii") + b"\x01", hashlib.sha256).digest()


def generate_recovery_code() -> bytes:
    """A fresh 256-bit recovery code from the OS CSPRNG."""
    return os.urandom(RECOVERY_MIN_CODE_BYTES)


def derive_recovery_factors(recovery_code: bytes) -> dict:
    """``{recovery_pub, kek_recovery_seed}`` from the raw code.

    The signing seed is derived, used, and dropped; only the public half and
    the KEK seed the caller needs come back.
    """
    if (
        not isinstance(recovery_code, (bytes, bytearray))
        or len(recovery_code) < RECOVERY_MIN_CODE_BYTES
    ):
        raise MalformedError(
            f"recovery code must be at least {RECOVERY_MIN_CODE_BYTES} bytes"
        )
    ikm = bytes(recovery_code)
    sign_seed = _hkdf32(ikm, RECOVERY_SIGN_INFO)
    return {
        "recovery_pub": KeyPair.from_private_hex(sign_seed.hex()).public_hex,
        "kek_recovery_seed": _hkdf32(ikm, RECOVERY_KEK_INFO),
    }


def recovery_signing_key(recovery_code: bytes) -> KeyPair:
    """The Ed25519 recovery keypair the code yields (co-signs ``key.rotate``)."""
    if (
        not isinstance(recovery_code, (bytes, bytearray))
        or len(recovery_code) < RECOVERY_MIN_CODE_BYTES
    ):
        raise MalformedError(
            f"recovery code must be at least {RECOVERY_MIN_CODE_BYTES} bytes"
        )
    return KeyPair.from_private_hex(
        _hkdf32(bytes(recovery_code), RECOVERY_SIGN_INFO).hex()
    )


def member_recovery_key(recovery_code: bytes, genesis_id: str) -> KeyPair:
    """A member's PER-ORGANIZATION recovery keypair (auto-c3yl1 item 2).

    This is the ``recovery_pub`` a member enrols in ``member.claim`` and the key
    that co-signs ``rekey_recovery_input`` to move a stolen persona off. It is
    derived from the recovery code folded with the organization's ``genesis_id``,
    so the same code yields a DIFFERENT key in every org — an identical value
    never links a member's personas across organizations. Domain-separated from
    :func:`recovery_signing_key` (the org-root recovery key), so the two are
    never the same key even in one org.

    The public half is what enrolls; the private half never leaves the client.
    """
    if (
        not isinstance(recovery_code, (bytes, bytearray))
        or len(recovery_code) < RECOVERY_MIN_CODE_BYTES
    ):
        raise MalformedError(
            f"recovery code must be at least {RECOVERY_MIN_CODE_BYTES} bytes"
        )
    if not isinstance(genesis_id, str) or len(genesis_id) != _GENESIS_ID_HEX_LEN:
        raise MalformedError("genesis_id must be a 64-char hex string")
    info = f"{MEMBER_RECOVERY_SIGN_INFO}:{genesis_id}"
    return KeyPair.from_private_hex(_hkdf32(bytes(recovery_code), info).hex())


def member_recovery_pub(recovery_code: bytes, genesis_id: str) -> str:
    """The public half a member enrols — :func:`member_recovery_key` public hex."""
    return member_recovery_key(recovery_code, genesis_id).public_hex


def _base32_encode(data: bytes) -> str:
    bits = value = 0
    out = []
    for byte in data:
        value = (value << 8) | byte
        bits += 8
        while bits >= 5:
            out.append(_CROCKFORD[(value >> (bits - 5)) & 31])
            bits -= 5
    if bits:
        out.append(_CROCKFORD[(value << (5 - bits)) & 31])
    return "".join(out)


def _base32_decode(text: str) -> bytes:
    bits = value = 0
    out = bytearray()
    for ch in text:
        idx = _CROCKFORD.find(ch)
        if idx < 0:
            raise MalformedError(f"invalid recovery code character: {ch}")
        value = (value << 5) | idx
        bits += 5
        if bits >= 8:
            out.append((value >> (bits - 8)) & 0xFF)
            bits -= 8
    return bytes(out)


def _checksum_suffix(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return _CROCKFORD[digest[0] >> 3] + _CROCKFORD[((digest[0] & 7) << 2) | (digest[1] >> 6)]


def encode_recovery_code(code: bytes) -> str:
    """The printable form: Crockford base32 + a 2-char checksum, in fives."""
    if not isinstance(code, (bytes, bytearray)) or len(code) != RECOVERY_MIN_CODE_BYTES:
        raise MalformedError(f"recovery code must be {RECOVERY_MIN_CODE_BYTES} bytes")
    body = _base32_encode(bytes(code)) + _checksum_suffix(bytes(code))
    return "-".join(body[i:i + 5] for i in range(0, len(body), 5))


def decode_recovery_code(printable: str) -> bytes:
    """Read a printed code back, catching a typo BEFORE anything is derived.

    Case, spacing and grouping are irrelevant, and the glyphs Crockford
    excludes are mapped home, so a person transcribing O for 0 or I for 1
    still gets in.
    """
    if not isinstance(printable, str):
        raise MalformedError("recovery code must be a string")
    cleaned = (
        printable.upper()
        .replace(" ", "").replace("-", "").replace("\t", "").replace("\n", "")
        .replace("O", "0").replace("I", "1").replace("L", "1")
    )
    if len(cleaned) < 3:
        raise MalformedError("recovery code is too short")
    body, check = cleaned[:-2], cleaned[-2:]
    code = _base32_decode(body)
    if len(code) != RECOVERY_MIN_CODE_BYTES:
        raise MalformedError("recovery code has the wrong length")
    if _checksum_suffix(code) != check:
        raise MalformedError("recovery code checksum failed — check for a typo")
    return code
