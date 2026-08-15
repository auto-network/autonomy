"""Passphrase armor for the org root private key.

The ``autonomy.network.org-key`` Setting stores the org root Ed25519
private key as an *armored, passphrase-encrypted* blob (spec §6.2,
invariant I1: plaintext exists only in the operator's browser during
ceremonies). This module is the canonical armor implementation — the C1
create-org-identity ceremony produces it, the C2 sign-on ceremony's
browser side (``static/js/network-signon.mjs``) mirrors the decrypt path
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
import hashlib
import json
import os
import re
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


#: GCM ciphertext of the 32-byte seed: seed + 16-byte auth tag.
_CT_LEN = _SEED_LEN + 16

_ROOT_PUB_RE = re.compile(r"^[0-9a-f]{64}$")


def _b64_field(container: dict, key: str, *, length: int, what: str) -> bytes:
    value = container.get(key)
    if not isinstance(value, str):
        raise ArmorError(f"armor {what} must be a base64 string")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArmorError(f"armor {what} does not base64-decode: {exc}") from exc
    if len(raw) != length:
        raise ArmorError(f"armor {what} must decode to exactly {length} bytes")
    if base64.b64encode(raw).decode("ascii") != value:
        # One accepted byte form (anti-malleability): no alternate
        # paddings/alphabets that decode equal but store different.
        raise ArmorError(f"armor {what} is not canonical base64")
    return raw


def _armor_body(armor: str) -> dict:
    """Shared decode: BEGIN/END unwrap, canonical base64, duplicate-key-refusing
    JSON parse. Returns the raw dict; VERSION-SPECIFIC field-closure is the
    caller's job (``parse_armor`` for v1, ``parse_armor_v2`` for v2), so the
    one anti-malleability boundary is not duplicated across format versions.
    """
    if not isinstance(armor, str):
        raise ArmorError("armor must be a string")
    lines = [ln.strip() for ln in armor.strip().splitlines() if ln.strip()]
    if len(lines) < 3 or lines[0] != ARMOR_BEGIN or lines[-1] != ARMOR_END:
        raise ArmorError("armor is missing its BEGIN/END lines")

    def _no_dup_pairs(pairs):
        # json.loads is last-key-wins on duplicates, which would let a
        # clean-looking parse hide a shadowed field. One key, one value —
        # rejected at the parser boundary, not papered over downstream.
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise ArmorError(f"armor body has duplicate key {k!r}")
            obj[k] = v
        return obj

    try:
        body = base64.b64decode("".join(lines[1:-1]), validate=True)
        data = json.loads(body, object_pairs_hook=_no_dup_pairs)
    except (binascii.Error, ValueError) as exc:
        raise ArmorError(f"armor body does not decode: {exc}") from exc
    if not isinstance(data, dict):
        raise ArmorError("armor body must be a JSON object")
    return data


def parse_armor(armor: str) -> dict:
    """Parse the armor text into its inner dict without decrypting.

    STRICT: the decoded body must carry EXACTLY the canonical fields —
    ``{v, kdf{name, hash, iterations, salt}, cipher{name, iv}, root_pub,
    ct}`` — with valid formats and lengths. Unknown fields are rejected
    outright: the armor is the ONLY thing the org-key store persists, so
    a tolerated extra field would be a smuggling channel for plaintext
    key material riding inside an otherwise-valid armor (I1).

    LEGACY v1. v1 never shipped; this reader exists ONLY as a migration source
    (see :func:`migrate_v1_to_v2`) and is DELETED from the shipped product once
    the one-shot migration has run. Nothing writes v1 going forward.
    """
    data = _armor_body(armor)
    if set(data) != {"v", "kdf", "cipher", "root_pub", "ct"}:
        raise ArmorError(
            "armor body must carry exactly {v, kdf, cipher, root_pub, ct} — "
            f"got {sorted(data)}; unknown fields are refused (I1: the armor "
            "must not be a carrier for anything else)"
        )
    if data["v"] != ARMOR_VERSION:
        raise ArmorError(f"unsupported armor version: {data['v']!r}")
    kdf, cipher = data["kdf"], data["cipher"]
    if (
        not isinstance(kdf, dict)
        or set(kdf) != {"name", "hash", "iterations", "salt"}
        or kdf["name"] != "PBKDF2"
        or kdf["hash"] != "SHA-256"
        or type(kdf["iterations"]) is not int
        or not (_MIN_ITERATIONS <= kdf["iterations"] <= _MAX_ITERATIONS)
    ):
        raise ArmorError(
            "armor kdf must be exactly {name: PBKDF2, hash: SHA-256, "
            "iterations, salt} with sane iterations"
        )
    if (
        not isinstance(cipher, dict)
        or set(cipher) != {"name", "iv"}
        or cipher["name"] != "AES-256-GCM"
    ):
        raise ArmorError("armor cipher must be exactly {name: AES-256-GCM, iv}")
    if not isinstance(data["root_pub"], str) or not _ROOT_PUB_RE.match(data["root_pub"]):
        raise ArmorError("armor root_pub must be 64 lowercase hex chars")
    _b64_field(kdf, "salt", length=_SALT_LEN, what="kdf.salt")
    _b64_field(cipher, "iv", length=_IV_LEN, what="cipher.iv")
    _b64_field(data, "ct", length=_CT_LEN, what="ct")
    return data


def canonicalize_armor(armor: str) -> str:
    """Strict-parse *armor* and re-emit it in the one canonical byte form.

    Belt-and-suspenders for anything that PERSISTS an armor it did not
    mint itself (the dashboard org-key store): the output is rebuilt
    field-by-field from the strictly-parsed dict, so only the allowed
    fields can survive into storage regardless of how the input text was
    laid out.
    """
    data = parse_armor(armor)
    body = canonical_json(
        {
            "v": ARMOR_VERSION,
            "kdf": {
                "name": "PBKDF2",
                "hash": "SHA-256",
                "iterations": data["kdf"]["iterations"],
                "salt": data["kdf"]["salt"],
            },
            "cipher": {"name": "AES-256-GCM", "iv": data["cipher"]["iv"]},
            "root_pub": data["root_pub"],
            "ct": data["ct"],
        }
    )
    b64 = base64.b64encode(body).decode("ascii")
    return "\n".join([ARMOR_BEGIN, *textwrap.wrap(b64, 64), ARMOR_END])


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


# ── Armor v2: versioned multi-wrap envelope (master KEK under a factor list) ──
#
# v2 INVERTS the wrap. The 32-byte seed is sealed under a fresh random 256-bit
# MASTER KEK, and the master KEK is wrapped by a LIST of factors. Unit 1 ships
# the ``password`` factor (parity with v1's single passphrase wrap); the
# recovery-code and passkey factors are added at their own slots in later units
# without another format change.
#
# This is the deliberate I1 re-expression the operator ruled on: the strict
# field-closure that v1 enforced with flat set-equality is preserved
# RECURSIVELY and gated by version — the top level, ``kek_seal``, and every
# ``factors[i]`` are each closed to an exact key set, and the factor ``type`` is
# a closed registry — so no tolerated field can ride inside at any level. AEAD
# associated data binds the version, ``root_pub``, and (for a factor wrap) the
# factor type, so material minted for one slot/version can't verify in another.
#
# FORWARD-COMPATIBLE BY CONSTRUCTION: version dispatch plus a migration registry
# make a future v3 a routine (parser, decryptor, (2,3)-migration) addition, not
# another constitutional event. V2 is the ONLY format ever WRITTEN going
# forward; v1 survives only as a read-once migration source (:func:`parse_armor`
# / :func:`decrypt_root_key`) and is deleted from the shipped product after the
# one-shot migration runs — v1 never shipped, so no persistent V1 path is kept.

ARMOR_VERSION_2 = 2
_MASTER_KEK_LEN = 32
#: AES-256-GCM ciphertext of a 32-byte payload (seed, or master KEK): 32 + tag.
_WRAP_LEN = _MASTER_KEK_LEN + 16
_V2_SEAL_AAD = b"autonomy.idkit.armor.v2.kek-seal\n"
_V2_FACTOR_AAD = b"autonomy.idkit.armor.v2.factor\n"


#: What each factor type contributes to the set commitment: its type plus the
#: PUBLIC material that identifies it. A registry, like the parser table, so a
#: new factor type cannot be added without deciding what pins it -- a type that
#: contributed nothing would be one an attacker could add or strip unnoticed,
#: which is the whole hole this closes.
_FACTOR_COMMITMENTS = {
    "password": lambda f: {
        "type": "password",
        "salt": f["kdf"]["salt"],
        "iterations": f["kdf"]["iterations"],
    },
    "recovery": lambda f: {"type": "recovery", "kem_pub": f["kem_pub"]},
}


def _factor_commitment(factors: list) -> str:
    """A digest over the factor SET, bound into the seal.

    Every factor's wrap is already bound to its own slot, so nobody can move
    material between slots. Nothing bound the LIST, though -- so anyone who
    could write the file could delete a factor, and the armor would still
    parse and still open with whatever remained. Stripping a recovery factor
    that way is silent: the owner finds out when they reach for the code, on
    the day they have already lost everything else.

    Sorted by type, so the commitment is over a SET rather than an ordering.
    """
    items = []
    for f in factors:
        contribute = _FACTOR_COMMITMENTS.get(f["type"])
        if contribute is None:
            raise ArmorError(
                f"v2 factor type {f['type']!r} has no set commitment; a factor "
                "that pins nothing could be added or stripped unnoticed"
            )
        items.append(contribute(f))
    items.sort(key=lambda d: d["type"])
    return hashlib.sha256(canonical_json(items)).hexdigest()


def _v2_seal_aad(root_pub: str, factors: list) -> bytes:
    """Bind the identity AND the exact set of factors into the seed's seal.

    Editing the factor list therefore invalidates the seal, so the armor fails
    to open rather than opening with a lock quietly missing. Fail-closed is
    the point: an altered file is detected, not silently honoured.
    """
    return (
        _V2_SEAL_AAD
        + root_pub.encode("ascii")
        + b"\n"
        + _factor_commitment(factors).encode("ascii")
    )


def _v2_factor_aad(root_pub: str, factor_type: str) -> bytes:
    return (
        _V2_FACTOR_AAD + root_pub.encode("ascii") + b"\n" + factor_type.encode("ascii")
    )


def encrypt_root_key_v2(
    key: KeyPair,
    passphrase: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
) -> str:
    """Armor *key* in the v2 envelope under a single ``password`` factor.

    Fresh random master KEK; the seed is sealed under it, and the master KEK is
    wrapped under ``KDF(passphrase)``. Returns the BEGIN/END-wrapped text. This
    is the only writer going forward (v1 is never written)."""
    if not (_MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS):
        raise ArmorError(
            f"iterations must be in [{_MIN_ITERATIONS}, {_MAX_ITERATIONS}]"
        )
    root_pub = key.public_hex
    # No zeroization here: Python bytes are immutable and cannot be reliably
    # wiped (same as v1). The browser/JS mirror — where the real ceremony runs —
    # zeroes its seed and master-KEK copies; this canonical Python path backs
    # tests, fixtures, and the one-shot migration.
    seed = bytes.fromhex(key.private_hex)
    master_kek = os.urandom(_MASTER_KEK_LEN)
    salt = os.urandom(_SALT_LEN)
    pw_key = _derive_key(passphrase, salt, iterations)
    wrap_iv = os.urandom(_IV_LEN)
    wrap = AESGCM(pw_key).encrypt(
        wrap_iv, master_kek, _v2_factor_aad(root_pub, "password")
    )
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
    # Sealed LAST: the seal commits to the factor set, so the set must exist.
    seal_iv = os.urandom(_IV_LEN)
    seal_ct = AESGCM(master_kek).encrypt(
        seal_iv, seed, _v2_seal_aad(root_pub, factors)
    )
    body = canonical_json(
        {
            "v": ARMOR_VERSION_2,
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
    return "\n".join([ARMOR_BEGIN, *textwrap.wrap(b64, 64), ARMOR_END])


#: The one purpose a recovery wrap is ever sealed for. The seal's own info
#: binds it, so material sealed for anything else cannot open an armor.
RECOVERY_ARMOR_PURPOSE = "autonomy/recovery-armor/v1"
#: suite id (1) + X25519 encapsulated key (32) + ChaCha20-Poly1305 of a 32-byte
#: master KEK (32 + 16 tag).
_RECOVERY_SEAL_LEN = 1 + 32 + _MASTER_KEK_LEN + 16


def _parse_recovery_factor(f: dict, index: int) -> None:
    if set(f) != {"type", "kem_pub", "sealed"}:
        raise ArmorError(
            f"v2 recovery factor[{index}] must carry exactly "
            "{type, kem_pub, sealed}"
        )
    if not isinstance(f["kem_pub"], str) or not _ROOT_PUB_RE.match(f["kem_pub"]):
        raise ArmorError("v2 recovery factor kem_pub must be 64 lowercase hex chars")
    _b64_field(f, "sealed", length=_RECOVERY_SEAL_LEN, what="recovery.sealed")


def _parse_password_factor(f: dict, index: int) -> None:
    if set(f) != {"type", "kdf", "cipher", "iv", "wrap"}:
        raise ArmorError(
            f"v2 password factor[{index}] must carry exactly "
            "{type, kdf, cipher, iv, wrap}"
        )
    kdf = f["kdf"]
    if (
        not isinstance(kdf, dict)
        or set(kdf) != {"name", "hash", "iterations", "salt"}
        or kdf["name"] != "PBKDF2"
        or kdf["hash"] != "SHA-256"
        or type(kdf["iterations"]) is not int
        or not (_MIN_ITERATIONS <= kdf["iterations"] <= _MAX_ITERATIONS)
    ):
        raise ArmorError(
            "v2 password factor kdf must be exactly {name: PBKDF2, hash: "
            "SHA-256, iterations, salt} with sane iterations"
        )
    if f["cipher"] != "AES-256-GCM":
        raise ArmorError("v2 password factor cipher must be AES-256-GCM")
    _b64_field(kdf, "salt", length=_SALT_LEN, what="factor.kdf.salt")
    _b64_field(f, "iv", length=_IV_LEN, what="factor.iv")
    _b64_field(f, "wrap", length=_WRAP_LEN, what="factor.wrap")


# Factor-type dispatch is TOTAL BY CONSTRUCTION: the registry IS the parser
# table, so a type cannot be "known" without a strict parser. Adding a factor
# type (recovery, passkey) means adding its parser here — there is no path where
# a registered type clears the membership check yet hits no field-closure, which
# would reopen I1 at the exact growth point this envelope exists for.
_FACTOR_PARSERS = {
    "password": _parse_password_factor,
    "recovery": _parse_recovery_factor,
}
#: Closed registry of factor types (unit 1); derived so it cannot drift from the
#: parsers. recovery/passkey add a (type -> strict parser) entry above.
_KNOWN_FACTOR_TYPES = frozenset(_FACTOR_PARSERS)


def parse_armor_v2(armor: str) -> dict:
    """Strict, RECURSIVELY closed parse of the v2 envelope (I1 preserved).

    Exact key sets at every level; a closed factor-``type`` registry; no
    duplicate factor types; valid formats and lengths. Any extra key, unknown
    factor type, or bad length is refused — the same no-smuggling guarantee as
    v1, re-expressed for a richer shape."""
    data = _armor_body(armor)
    if set(data) != {"v", "root_pub", "kek_seal", "factors"}:
        raise ArmorError(
            "v2 armor body must carry exactly {v, root_pub, kek_seal, factors} — "
            f"got {sorted(data)}; unknown fields are refused (I1)"
        )
    if data["v"] != ARMOR_VERSION_2:
        raise ArmorError(f"parse_armor_v2 got version {data['v']!r}, expected 2")
    if not isinstance(data["root_pub"], str) or not _ROOT_PUB_RE.match(data["root_pub"]):
        raise ArmorError("v2 armor root_pub must be 64 lowercase hex chars")
    seal = data["kek_seal"]
    if (
        not isinstance(seal, dict)
        or set(seal) != {"cipher", "iv", "ct"}
        or seal["cipher"] != "AES-256-GCM"
    ):
        raise ArmorError("v2 kek_seal must be exactly {cipher: AES-256-GCM, iv, ct}")
    _b64_field(seal, "iv", length=_IV_LEN, what="kek_seal.iv")
    _b64_field(seal, "ct", length=_WRAP_LEN, what="kek_seal.ct")
    factors = data["factors"]
    if not isinstance(factors, list) or not factors:
        raise ArmorError("v2 factors must be a non-empty list")
    seen = set()
    for i, f in enumerate(factors):
        if not isinstance(f, dict) or "type" not in f:
            raise ArmorError(f"v2 factor[{i}] must be an object with a type")
        ftype = f["type"]
        parser = _FACTOR_PARSERS.get(ftype)
        if parser is None:
            raise ArmorError(
                f"v2 factor[{i}] has unknown type {ftype!r}; the registry is "
                f"closed to {sorted(_FACTOR_PARSERS)}"
            )
        if ftype in seen:
            raise ArmorError(f"v2 has a duplicate factor type {ftype!r}")
        seen.add(ftype)
        parser(f, i)  # total dispatch — a known type always has a strict parser
    return data


def decrypt_root_key_v2(armor: str, passphrase: str) -> KeyPair:
    """Open a v2 armor with *passphrase*: unwrap the master KEK from the
    password factor, then unseal the seed under the master KEK."""
    data = parse_armor_v2(armor)
    root_pub = data["root_pub"]
    master_kek = _v2_master_kek(data, passphrase)
    try:
        seal_iv = base64.b64decode(data["kek_seal"]["iv"], validate=True)
        seal_ct = base64.b64decode(data["kek_seal"]["ct"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArmorError(f"v2 armor fields do not base64-decode: {exc}") from exc
    return _open_seed_under_master_kek(
        master_kek, seal_iv, seal_ct, root_pub, data["factors"]
    )


def _reseal_to_factor_set(
    data: dict, master_kek: bytes, previous_factors: list
) -> None:
    """Re-seal the seed so it commits to ``data['factors']`` as it now stands.

    Called by every operation that legitimately changes the set. It needs the
    master KEK, which is exactly the authorisation that separates an owner
    editing their own locks from someone editing the file behind their back.
    """
    root_pub = data["root_pub"]
    try:
        old_iv = base64.b64decode(data["kek_seal"]["iv"], validate=True)
        old_ct = base64.b64decode(data["kek_seal"]["ct"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArmorError(f"v2 armor fields do not base64-decode: {exc}") from exc
    # Recover the seed under the CURRENT commitment, then commit to the new one.
    seed = _open_seed_under_master_kek(
        master_kek, old_iv, old_ct, root_pub, previous_factors
    )
    seal_iv = os.urandom(_IV_LEN)
    seal_ct = AESGCM(master_kek).encrypt(
        seal_iv,
        bytes.fromhex(seed.private_hex),
        _v2_seal_aad(root_pub, data["factors"]),
    )
    data["kek_seal"] = {
        "cipher": "AES-256-GCM",
        "iv": base64.b64encode(seal_iv).decode("ascii"),
        "ct": base64.b64encode(seal_ct).decode("ascii"),
    }


def _emit_v2(data: dict) -> str:
    """Re-serialize a parsed v2 body as armor text (canonical, wrapped)."""
    b64 = base64.b64encode(canonical_json(data)).decode("ascii")
    return "\n".join([ARMOR_BEGIN, *textwrap.wrap(b64, 64), ARMOR_END])


def add_recovery_factor(armor: str, passphrase: str, recovery_kem_pub: str) -> str:
    """Enroll a recovery factor, knowing only the code's PUBLIC half.

    The recovery code itself stays COLD: it never enters this process. Its
    public encapsulation key is enough to seal the master KEK to it, which is
    what makes the printed code able to open this armor ALONE later --- the
    bearer principle. A recovery code that needed a surviving factor to work
    would not be a recovery code.

    *passphrase* is needed to reach the master KEK, and to re-seal the seed:
    the seal commits to the factor SET, so changing the set means re-sealing
    it. That is what makes the set tamper-evident -- only somebody who can
    already open this armor can change which locks it has. Every existing
    factor keeps working, and the identity inside is untouched.
    """
    if not isinstance(recovery_kem_pub, str) or not _ROOT_PUB_RE.match(recovery_kem_pub):
        raise ArmorError("recovery_kem_pub must be 64 lowercase hex chars")
    from .sealing import seal

    data = parse_armor_v2(armor)
    if any(f["type"] == "recovery" for f in data["factors"]):
        raise ArmorError(
            "this armor already carries a recovery factor; replacing one is a "
            "rotation, not an addition"
        )
    master_kek = _v2_master_kek(data, passphrase)
    previous_factors = list(data["factors"])
    sealed = seal(master_kek, recovery_kem_pub, RECOVERY_ARMOR_PURPOSE)
    if len(sealed) != _RECOVERY_SEAL_LEN:
        raise ArmorError("sealed recovery wrap is not the expected length")
    data["factors"] = [
        *data["factors"],
        {
            "type": "recovery",
            "kem_pub": recovery_kem_pub,
            "sealed": base64.b64encode(sealed).decode("ascii"),
        },
    ]
    _reseal_to_factor_set(data, master_kek, previous_factors)
    return _emit_v2(parse_armor_v2(_emit_v2(data)))


def remove_factor(armor: str, passphrase: str, factor_type: str) -> str:
    """Drop a lock from this armor.

    Authorised by the passphrase, which is what makes this different from
    someone editing the file: changing the set re-seals the seed, and that
    needs the master KEK. The last lock can never be removed --- an armor
    with no way in is not a safer armor, it is a destroyed identity.

    WHAT THIS DOES NOT ACHIEVE, and it matters: dropping a weak lock does
    not reach the copies of this file that already exist. Anyone holding an
    older copy still has the weak lock and the identity behind it. Removing
    a factor is only a real strengthening if the identity is ROTATED too ---
    see :mod:`tools.network.idkit.root_rotation`. On its own it is
    housekeeping, not security.
    """
    data = parse_armor_v2(armor)
    if factor_type not in _FACTOR_PARSERS:
        raise ArmorError(
            f"unknown factor type {factor_type!r}; this armor knows "
            f"{sorted(_FACTOR_PARSERS)}"
        )
    if not any(f["type"] == factor_type for f in data["factors"]):
        raise ArmorError(f"this armor carries no {factor_type!r} factor to remove")
    remaining = [f for f in data["factors"] if f["type"] != factor_type]
    if not remaining:
        raise ArmorError(
            "refusing to remove the last factor: an armor nothing can open is "
            "a destroyed identity, not a hardened one"
        )
    master_kek = _v2_master_kek(data, passphrase)
    previous_factors = list(data["factors"])
    data["factors"] = remaining
    _reseal_to_factor_set(data, master_kek, previous_factors)
    return _emit_v2(parse_armor_v2(_emit_v2(data)))


def armor_factor_types(armor: str) -> list:
    """Which locks this armor carries, in declaration order."""
    return [f["type"] for f in parse_armor_v2(armor)["factors"]]


def decrypt_root_key_with_recovery(armor: str, recovery_code: bytes) -> KeyPair:
    """Open a v2 armor with the printed recovery code ALONE.

    This is the whole point of the code: you reach for it precisely when the
    factor you normally use is gone, so nothing else may be required. The
    code's KEK half reconstructs the private encapsulation key, which unseals
    the master KEK, which unseals the identity.
    """
    from .recovery import derive_recovery_factors
    from .sealing import derive_encapsulation_keypair
    from .sealing import open as seal_open

    data = parse_armor_v2(armor)
    root_pub = data["root_pub"]
    factor = next((f for f in data["factors"] if f["type"] == "recovery"), None)
    if factor is None:
        raise ArmorError(
            "this armor carries no recovery factor; a recovery code cannot open it"
        )
    kek_seed = derive_recovery_factors(recovery_code)["kek_recovery_seed"]
    private_hex, public_hex = derive_encapsulation_keypair(
        kek_seed, RECOVERY_ARMOR_PURPOSE
    )
    if public_hex != factor["kem_pub"]:
        raise ArmorPassphraseError(
            "that recovery code does not match this armor's recovery factor"
        )
    try:
        sealed = base64.b64decode(factor["sealed"], validate=True)
        seal_iv = base64.b64decode(data["kek_seal"]["iv"], validate=True)
        seal_ct = base64.b64decode(data["kek_seal"]["ct"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArmorError(f"v2 armor fields do not base64-decode: {exc}") from exc
    try:
        master_kek = seal_open(sealed, private_hex, RECOVERY_ARMOR_PURPOSE)
    except Exception as exc:
        raise ArmorPassphraseError(
            "the recovery factor does not open with that code"
        ) from exc
    return _open_seed_under_master_kek(
        master_kek, seal_iv, seal_ct, root_pub, data["factors"]
    )


def recover_and_reset_password(
    armor: str,
    recovery_code: bytes,
    new_passphrase: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
) -> str:
    """Open with the recovery code and immediately re-establish the password.

    You reach for a recovery code because a factor is GONE, so authenticating
    is only half the job: an armor you can open only with the printed code has
    simply moved the single point of failure. This re-wraps the master KEK
    under a fresh password in the same act.

    The seed is never re-sealed and the master KEK is unchanged, so the
    recovery factor keeps working and the identity is untouched.

    NOTE what this deliberately does NOT do: the previous armor file, with the
    OLD password, still opens this identity for anyone holding a copy. Making
    an old copy worthless is a ROOT ROTATION, not a factor reset --- they are
    different operations and only one of them is forward secrecy.
    """
    if not (_MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS):
        raise ArmorError(
            f"iterations must be in [{_MIN_ITERATIONS}, {_MAX_ITERATIONS}]"
        )
    from .recovery import derive_recovery_factors
    from .sealing import derive_encapsulation_keypair
    from .sealing import open as seal_open

    data = parse_armor_v2(armor)
    root_pub = data["root_pub"]
    factor = next((f for f in data["factors"] if f["type"] == "recovery"), None)
    if factor is None:
        raise ArmorError(
            "this armor carries no recovery factor; a recovery code cannot open it"
        )
    kek_seed = derive_recovery_factors(recovery_code)["kek_recovery_seed"]
    private_hex, public_hex = derive_encapsulation_keypair(
        kek_seed, RECOVERY_ARMOR_PURPOSE
    )
    if public_hex != factor["kem_pub"]:
        raise ArmorPassphraseError(
            "that recovery code does not match this armor's recovery factor"
        )
    try:
        master_kek = seal_open(
            base64.b64decode(factor["sealed"], validate=True),
            private_hex,
            RECOVERY_ARMOR_PURPOSE,
        )
    except Exception as exc:
        raise ArmorPassphraseError(
            "the recovery factor does not open with that code"
        ) from exc

    salt = os.urandom(_SALT_LEN)
    wrap_iv = os.urandom(_IV_LEN)
    pw_key = _derive_key(new_passphrase, salt, iterations)
    wrap = AESGCM(pw_key).encrypt(
        wrap_iv, master_kek, _v2_factor_aad(root_pub, "password")
    )
    password_factor = {
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
    previous_factors = list(data["factors"])
    data["factors"] = [
        password_factor if f["type"] == "password" else f for f in data["factors"]
    ]
    if not any(f["type"] == "password" for f in data["factors"]):
        data["factors"] = [password_factor, *data["factors"]]
    # The password factor's salt is part of the set commitment, so replacing
    # it changes the set and the seed must be re-sealed to the new one.
    _reseal_to_factor_set(data, master_kek, previous_factors)
    return _emit_v2(parse_armor_v2(_emit_v2(data)))


def _open_seed_under_master_kek(
    master_kek: bytes, seal_iv: bytes, seal_ct: bytes, root_pub: str, factors: list
) -> KeyPair:
    """The last step every factor shares: master KEK -> seed -> identity.

    The seal commits to the factor set, so an armor whose list has been edited
    fails HERE -- loudly -- rather than opening with a lock quietly removed.
    """
    try:
        seed = AESGCM(master_kek).decrypt(
            seal_iv, seal_ct, _v2_seal_aad(root_pub, factors)
        )
    except InvalidTag as exc:
        raise MalformedError(
            "v2 master KEK does not open the seed seal -- the factor list "
            "may have been altered"
        ) from exc
    if len(seed) != _SEED_LEN:
        raise MalformedError("v2 armor plaintext is not a 32-byte Ed25519 seed")
    key = KeyPair.from_private_hex(seed.hex())
    if key.public_hex != root_pub:
        raise MalformedError("v2 armor root_pub does not match the enclosed private key")
    return key


def _v2_master_kek(data: dict, passphrase: str) -> bytes:
    """The master KEK, unwrapped from the password factor of a parsed body."""
    root_pub = data["root_pub"]
    pw = next((f for f in data["factors"] if f["type"] == "password"), None)
    if pw is None:
        raise ArmorError("v2 armor has no password factor to open with a passphrase")
    try:
        salt = base64.b64decode(pw["kdf"]["salt"], validate=True)
        wrap_iv = base64.b64decode(pw["iv"], validate=True)
        wrap = base64.b64decode(pw["wrap"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArmorError(f"v2 armor fields do not base64-decode: {exc}") from exc
    pw_key = _derive_key(passphrase, salt, pw["kdf"]["iterations"])
    try:
        return AESGCM(pw_key).decrypt(
            wrap_iv, wrap, _v2_factor_aad(root_pub, "password")
        )
    except InvalidTag as exc:
        raise ArmorPassphraseError("v2 armor does not open with that passphrase") from exc


# Version dispatch tables — a future v3 slots in here (parser, decryptor, and a
# (2, 3) migration) with no change to callers. This is the forward-compatibility
# the operator required: new formats are routine, never constitutional.
_ARMOR_DECRYPTORS = {
    ARMOR_VERSION: decrypt_root_key,      # LEGACY v1 — removed post-migration
    ARMOR_VERSION_2: decrypt_root_key_v2,
}


_ARMOR_PARSERS = {ARMOR_VERSION: parse_armor, ARMOR_VERSION_2: parse_armor_v2}


def armor_version(armor: str) -> int:
    """The declared version of *armor* (1 or 2), else :class:`ArmorError`."""
    data = _armor_body(armor)
    v = data.get("v")
    if v not in _ARMOR_DECRYPTORS:
        raise ArmorError(f"unsupported armor version: {v!r}")
    return v


def armor_root_pub(armor: str) -> str:
    """The bound ``root_pub`` of a v1 OR v2 armor, WITHOUT decrypting.

    For the server-side call sites that only need the identity, not the key —
    version-agnostic replacement for the v1-only ``parse_armor(armor)["root_pub"]``.
    Strict-parses per version (so a malformed blob is still refused)."""
    return _ARMOR_PARSERS[armor_version(armor)](armor)["root_pub"]


def decrypt_root_key_any(armor: str, passphrase: str) -> KeyPair:
    """Open a v1 OR v2 armor. The v1 branch exists ONLY to read a pre-migration
    blob and is deleted from the shipped product once migration has run."""
    return _ARMOR_DECRYPTORS[armor_version(armor)](armor, passphrase)


def migrate_v1_to_v2(
    v1_armor: str,
    passphrase: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
) -> str:
    """One-shot: open a v1 armor and re-emit it as v2. THROWAWAY — this function
    and the v1 reader are deleted from the shipped product after the migration
    runs (v1 never shipped, so no persistent V1 path is kept).

    Produces the v2 text ONLY. The caller MUST replace-and-destroy the v1 blob
    ATOMICALLY with the v2 write — no window that leaves a readable v1 master-KEK
    copy, and no v1 copy left in any backup/sync path it can reach. A leftover v1
    blob is a master-KEK sealed under the weaker single-PBKDF2 wrap that an
    attacker can brute-force offline with their own reader, so the destroy is
    load-bearing, not hygiene."""
    if armor_version(v1_armor) != ARMOR_VERSION:
        raise ArmorError("migrate_v1_to_v2 expects a v1 armor")
    key = decrypt_root_key(v1_armor, passphrase)
    return encrypt_root_key_v2(key, passphrase, iterations=iterations)
