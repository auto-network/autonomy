"""Passphrase armor for the org root private key.

The ``autonomy.network.org-key`` Setting stores the org root Ed25519
private key as an *armored, passphrase-encrypted* blob (spec §6.2,
invariant I1: plaintext exists only in the operator's browser during
ceremonies). This module is the canonical armor implementation — the C1
create-org-identity ceremony produces it, the C2 sign-on ceremony's
browser side (``static/js/network-signon.js``) mirrors the decrypt path
in WebCrypto, and tests use it to build fixtures. One format, two
implementations, cross-checked by the L2.B sweep.

Format::

    -----BEGIN AUTONOMY NETWORK ROOT KEY-----
    base64( canonical_json({
      "v": 1,
      "kdf": {"name": "PBKDF2", "hash": "SHA-256",
               "iterations": N, "salt": <b64 16 bytes>},
      "cipher": {"name": "AES-256-GCM", "iv": <b64 12 bytes>},
      "root_pub": <64 hex>,
      "ct": <b64: AES-GCM ciphertext of the raw 32-byte seed>
    }) )
    -----END AUTONOMY NETWORK ROOT KEY-----

The AES-GCM AAD binds the ciphertext to both the format version and
``root_pub``, so a blob cannot be re-labelled as a different org's key
without failing authentication. A wrong passphrase surfaces as GCM
authentication failure (:class:`ArmorPassphraseError`) — there is no
oracle distinguishing "wrong passphrase" from "corrupted blob", which is
fine: both mean "this armor does not open with that passphrase".
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import textwrap

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .canonical import canonical_json
from .errors import IdkitError, MalformedError
from .keys import KeyPair

ARMOR_BEGIN = "-----BEGIN AUTONOMY NETWORK ROOT KEY-----"
ARMOR_END = "-----END AUTONOMY NETWORK ROOT KEY-----"
ARMOR_VERSION = 1
ARMOR_AAD_PREFIX = b"autonomy.idkit.armor.v1\n"

#: PBKDF2-SHA256 work factor. High enough to make offline guessing of a
#: stolen blob expensive, low enough that the browser-side ceremony stays
#: interactive (~0.5 s in current Chrome).
DEFAULT_ITERATIONS = 600_000
_MIN_ITERATIONS = 10_000
_MAX_ITERATIONS = 100_000_000

_SALT_LEN = 16
_IV_LEN = 12
_SEED_LEN = 32


class ArmorError(IdkitError):
    """The armor cannot be parsed or decrypted."""


class ArmorPassphraseError(ArmorError):
    """GCM authentication failed — wrong passphrase or corrupted blob."""


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise ArmorError("passphrase must be a non-empty string")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(passphrase.encode("utf-8"))


def _aad(root_pub: str) -> bytes:
    return ARMOR_AAD_PREFIX + root_pub.encode("ascii")


def encrypt_root_key(
    key: KeyPair,
    passphrase: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
) -> str:
    """Armor *key* under *passphrase*; returns the BEGIN/END-wrapped text."""
    if not (_MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS):
        raise ArmorError(
            f"iterations must be in [{_MIN_ITERATIONS}, {_MAX_ITERATIONS}]"
        )
    salt = os.urandom(_SALT_LEN)
    iv = os.urandom(_IV_LEN)
    aes_key = _derive_key(passphrase, salt, iterations)
    seed = bytes.fromhex(key.private_hex)
    ct = AESGCM(aes_key).encrypt(iv, seed, _aad(key.public_hex))
    body = canonical_json(
        {
            "v": ARMOR_VERSION,
            "kdf": {
                "name": "PBKDF2",
                "hash": "SHA-256",
                "iterations": iterations,
                "salt": base64.b64encode(salt).decode("ascii"),
            },
            "cipher": {
                "name": "AES-256-GCM",
                "iv": base64.b64encode(iv).decode("ascii"),
            },
            "root_pub": key.public_hex,
            "ct": base64.b64encode(ct).decode("ascii"),
        }
    )
    b64 = base64.b64encode(body).decode("ascii")
    return "\n".join([ARMOR_BEGIN, *textwrap.wrap(b64, 64), ARMOR_END])


def parse_armor(armor: str) -> dict:
    """Parse the armor text into its inner dict without decrypting."""
    if not isinstance(armor, str):
        raise ArmorError("armor must be a string")
    lines = [ln.strip() for ln in armor.strip().splitlines() if ln.strip()]
    if len(lines) < 3 or lines[0] != ARMOR_BEGIN or lines[-1] != ARMOR_END:
        raise ArmorError("armor is missing its BEGIN/END lines")
    try:
        body = base64.b64decode("".join(lines[1:-1]), validate=True)
        data = json.loads(body)
    except (binascii.Error, ValueError) as exc:
        raise ArmorError(f"armor body does not decode: {exc}") from exc
    if not isinstance(data, dict):
        raise ArmorError("armor body must be a JSON object")
    if data.get("v") != ARMOR_VERSION:
        raise ArmorError(f"unsupported armor version: {data.get('v')!r}")
    kdf, cipher = data.get("kdf"), data.get("cipher")
    if (
        not isinstance(kdf, dict)
        or kdf.get("name") != "PBKDF2"
        or kdf.get("hash") != "SHA-256"
        or type(kdf.get("iterations")) is not int
        or not (_MIN_ITERATIONS <= kdf["iterations"] <= _MAX_ITERATIONS)
        or not isinstance(kdf.get("salt"), str)
    ):
        raise ArmorError("armor kdf must be PBKDF2/SHA-256 with sane iterations and a salt")
    if (
        not isinstance(cipher, dict)
        or cipher.get("name") != "AES-256-GCM"
        or not isinstance(cipher.get("iv"), str)
    ):
        raise ArmorError("armor cipher must be AES-256-GCM with an iv")
    if not isinstance(data.get("root_pub"), str) or not isinstance(data.get("ct"), str):
        raise ArmorError("armor must carry root_pub and ct")
    return data


def decrypt_root_key(armor: str, passphrase: str) -> KeyPair:
    """Open the armor with *passphrase*; returns the root :class:`KeyPair`.

    Raises :class:`ArmorPassphraseError` when GCM authentication fails
    (wrong passphrase or tampered blob) and :class:`ArmorError` /
    :class:`MalformedError` for structural problems.
    """
    data = parse_armor(armor)
    try:
        salt = base64.b64decode(data["kdf"]["salt"], validate=True)
        iv = base64.b64decode(data["cipher"]["iv"], validate=True)
        ct = base64.b64decode(data["ct"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArmorError(f"armor fields do not base64-decode: {exc}") from exc
    aes_key = _derive_key(passphrase, salt, data["kdf"]["iterations"])
    try:
        seed = AESGCM(aes_key).decrypt(iv, ct, _aad(data["root_pub"]))
    except InvalidTag as exc:
        raise ArmorPassphraseError(
            "armor does not open with that passphrase"
        ) from exc
    if len(seed) != _SEED_LEN:
        raise MalformedError("armor plaintext is not a 32-byte Ed25519 seed")
    key = KeyPair.from_private_hex(seed.hex())
    if key.public_hex != data["root_pub"]:
        raise MalformedError("armor root_pub does not match the enclosed private key")
    return key
