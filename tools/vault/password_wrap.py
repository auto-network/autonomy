"""Password wrap for vault factor seeds — encrypt 32 bytes under a password.

This is the vault password factor's storage format and nothing else. It is
not identity armor: it protects a per-factor random seed (tools/vault/
factors.py), never a personal or organization root. The implementation was
extracted verbatim from the retired identity-armor module so that every
stored ``vault_factors`` row keeps opening byte-for-byte.

THE WIRE FORMAT IS FROZEN. The BEGIN/END lines, the ``v: 2`` format tag,
the AAD domain strings, and the field shapes are exactly what existing rows
carry; changing any of them orphans persisted factor seeds. The ``v2`` in
the domain strings names this envelope format's lineage, not the identity
armor generation — identity armor is the root factor policy and shares no
code with this module.

Construction: a fresh random master key seals the seed (AES-256-GCM); the
master key is wrapped under PBKDF2-HMAC-SHA256(password). The seal's AAD
commits to the factor list, so a tampered envelope fails closed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import textwrap

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from tools.network.idkit.canonical import canonical_json
from tools.network.idkit.keys import KeyPair

from .errors import FactorError

# ── Frozen wire-format constants (must match every stored row) ─────────────
_BEGIN = "-----BEGIN AUTONOMY NETWORK ROOT KEY-----"
_END = "-----END AUTONOMY NETWORK ROOT KEY-----"
_FORMAT_TAG = 2
_SEAL_AAD = b"autonomy.idkit.armor.v2.kek-seal\n"
_FACTOR_AAD = b"autonomy.idkit.armor.v2.factor\n"

DEFAULT_ITERATIONS = 600_000
_MIN_ITERATIONS = 10_000
_MAX_ITERATIONS = 100_000_000
_MASTER_KEY_LEN = 32
_SEED_LEN = 32
_SALT_LEN = 16
_IV_LEN = 12


class PasswordWrapError(FactorError):
    """The envelope is malformed, tampered, or the password is wrong."""


def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    if not isinstance(password, str) or not password:
        raise PasswordWrapError("password must be a non-empty string")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    )
    return kdf.derive(password.encode("utf-8"))


def _factor_aad(root_pub: str) -> bytes:
    return _FACTOR_AAD + root_pub.encode("ascii") + b"\npassword"


def _seal_aad(root_pub: str, factors: list) -> bytes:
    items = [
        {
            "type": "password",
            "salt": f["kdf"]["salt"],
            "iterations": f["kdf"]["iterations"],
        }
        for f in factors
    ]
    items.sort(key=canonical_json)
    commitment = hashlib.sha256(canonical_json(items)).hexdigest()
    return _SEAL_AAD + root_pub.encode("ascii") + b"\n" + commitment.encode("ascii")


def _body(envelope: str) -> dict:
    if not isinstance(envelope, str):
        raise PasswordWrapError("password wrap must be a string")
    lines = [ln.strip() for ln in envelope.strip().splitlines() if ln.strip()]
    if len(lines) < 3 or lines[0] != _BEGIN or lines[-1] != _END:
        raise PasswordWrapError("password wrap is missing its BEGIN/END lines")

    def _no_dup_pairs(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise PasswordWrapError(f"password wrap has duplicate key {k!r}")
            obj[k] = v
        return obj

    try:
        raw = base64.b64decode("".join(lines[1:-1]), validate=True)
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
    except (binascii.Error, ValueError) as exc:
        raise PasswordWrapError(f"password wrap body does not decode: {exc}") from exc
    if not isinstance(data, dict):
        raise PasswordWrapError("password wrap body must be an object")
    if set(data) != {"v", "root_pub", "kek_seal", "factors"}:
        raise PasswordWrapError(
            "password wrap body must carry exactly {v, root_pub, kek_seal, factors}"
        )
    if data["v"] != _FORMAT_TAG:
        raise PasswordWrapError(f"unsupported password wrap format: {data['v']!r}")
    factors = data["factors"]
    if (
        not isinstance(factors, list)
        or len(factors) != 1
        or not isinstance(factors[0], dict)
        or factors[0].get("type") != "password"
    ):
        raise PasswordWrapError(
            "password wrap must carry exactly one password factor"
        )
    return data


def wrap_password_factor(
    key: KeyPair, password: str, *, iterations: int = DEFAULT_ITERATIONS
) -> str:
    """Encrypt *key*'s 32-byte seed under *password*; return the envelope text."""
    if not (_MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS):
        raise PasswordWrapError(
            f"iterations must be in [{_MIN_ITERATIONS}, {_MAX_ITERATIONS}]"
        )
    root_pub = key.public_hex
    seed = bytes.fromhex(key.private_hex)
    master_key = os.urandom(_MASTER_KEY_LEN)
    salt = os.urandom(_SALT_LEN)
    pw_key = _derive_key(password, salt, iterations)
    wrap_iv = os.urandom(_IV_LEN)
    wrap = AESGCM(pw_key).encrypt(wrap_iv, master_key, _factor_aad(root_pub))
    factors = [
        {
            "type": "password",
            "kdf": {
                "name": "PBKDF2",
                "hash": "SHA-256",
                "iterations": iterations,
                "salt": base64.b64encode(salt).decode("ascii"),
            },
            "cipher": "AES-256-GCM",
            "iv": base64.b64encode(wrap_iv).decode("ascii"),
            "wrap": base64.b64encode(wrap).decode("ascii"),
        }
    ]
    seal_iv = os.urandom(_IV_LEN)
    seal_ct = AESGCM(master_key).encrypt(seal_iv, seed, _seal_aad(root_pub, factors))
    body = canonical_json(
        {
            "v": _FORMAT_TAG,
            "root_pub": root_pub,
            "kek_seal": {
                "cipher": "AES-256-GCM",
                "iv": base64.b64encode(seal_iv).decode("ascii"),
                "ct": base64.b64encode(seal_ct).decode("ascii"),
            },
            "factors": factors,
        }
    )
    b64 = base64.b64encode(body).decode("ascii")
    return "\n".join([_BEGIN, *textwrap.wrap(b64, 64), _END])


def open_password_factor(envelope: str, password: str) -> KeyPair:
    """Open a password wrap with *password*; return the enclosed keypair."""
    data = _body(envelope)
    root_pub = data["root_pub"]
    pw = data["factors"][0]
    try:
        salt = base64.b64decode(pw["kdf"]["salt"], validate=True)
        wrap_iv = base64.b64decode(pw["iv"], validate=True)
        wrap = base64.b64decode(pw["wrap"], validate=True)
        seal_iv = base64.b64decode(data["kek_seal"]["iv"], validate=True)
        seal_ct = base64.b64decode(data["kek_seal"]["ct"], validate=True)
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise PasswordWrapError(
            f"password wrap fields do not decode: {exc}"
        ) from exc
    pw_key = _derive_key(password, salt, pw["kdf"]["iterations"])
    try:
        master_key = AESGCM(pw_key).decrypt(wrap_iv, wrap, _factor_aad(root_pub))
    except InvalidTag as exc:
        raise PasswordWrapError(
            "password wrap does not open with that password"
        ) from exc
    try:
        seed = AESGCM(master_key).decrypt(
            seal_iv, seal_ct, _seal_aad(root_pub, data["factors"])
        )
    except InvalidTag as exc:
        raise PasswordWrapError(
            "password wrap seal does not open — the envelope may be altered"
        ) from exc
    if len(seed) != _SEED_LEN:
        raise PasswordWrapError("password wrap plaintext is not a 32-byte seed")
    key = KeyPair.from_private_hex(seed.hex())
    if key.public_hex != root_pub:
        raise PasswordWrapError(
            "password wrap root_pub does not match the enclosed key"
        )
    return key
