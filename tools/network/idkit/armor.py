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


#: A key id is exactly 64 lowercase hex characters. Shared by both the
#: parser and the seal AAD, so it outlives any one armor version.
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
    caller's job (``parse_armor`` for v1, ``parse_armor`` for v2), so the
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


def canonicalize_armor(armor: str) -> str:
    """Strict-parse a v2 armor and re-emit it in the one canonical byte form.

    Unlike v1 this does not rebuild the body field by field, because it does
    not need to: :func:`parse_armor` is closed to an EXACT key set at every
    level, including a per-type strict parser for each factor, so anything the
    parse accepted already contains nothing else. Re-emitting the parsed dict
    as canonical JSON is therefore complete by construction --- and stays
    complete when a factor type is added, which a hand-copied field list here
    would not.
    """
    return _emit_v2(parse_armor(armor))


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

ARMOR_VERSION = 2
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
    # A passkey factor is plural: one per full passkey, pinned by BOTH the
    # credential it belongs to and the encapsulation pub the master KEK is
    # sealed to. Both are public; both must be committed, so a factor cannot be
    # swapped for one that opens under a different device's ceremony unnoticed.
    "passkey": lambda f: {
        "type": "passkey",
        "credential_id": f["credential_id"],
        "kem_pub": f["kem_pub"],
    },
    # A combined (MFA) factor requires BOTH a password AND a passkey PRF to
    # reconstruct the master KEK (a 2-of-2 XOR split, neither half alone). It
    # pins the passkey it pairs with (credential_id + kem_pub) and the password
    # half's KDF (salt + iterations) — all public, all committed, so a combined
    # factor cannot be swapped for one that pairs a different device or a weaker
    # KDF unnoticed.
    "combined": lambda f: {
        "type": "combined",
        "credential_id": f["credential_id"],
        "kem_pub": f["kem_pub"],
        "salt": f["kdf"]["salt"],
        "iterations": f["kdf"]["iterations"],
    },
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
    # Sorted by the whole canonical item, not just type: passkey factors repeat
    # the type, so a type-only key would not be a total order over the set.
    items.sort(key=canonical_json)
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


def encrypt_root_key(
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
            "v": ARMOR_VERSION,
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
#: The one label a passkey factor is derived and sealed under — the SAME
#: vault-factor purpose the enrollment ceremony derives the provisioning key
#: under (auto-oox5r: one key, one label). So the public half a passkey
#: publishes in its enrollment statement IS the key the master KEK is sealed to
#: here, and a fresh PRF eval re-derives the private half that opens it.
PASSKEY_ARMOR_PURPOSE = "autonomy/vault-factor/v1"
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


#: The credential id a passkey factor belongs to — the WebAuthn rawId, carried
#: as base64url exactly as the enrollment statement and passkey row hold it.
_CREDENTIAL_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,256}\Z")


def _parse_passkey_factor(f: dict, index: int) -> None:
    if set(f) != {"type", "credential_id", "kem_pub", "sealed"}:
        raise ArmorError(
            f"v2 passkey factor[{index}] must carry exactly "
            "{type, credential_id, kem_pub, sealed}"
        )
    if not isinstance(f["credential_id"], str) or not _CREDENTIAL_ID_RE.match(
        f["credential_id"]
    ):
        raise ArmorError(
            "v2 passkey factor credential_id must be base64url (the WebAuthn rawId)"
        )
    if not isinstance(f["kem_pub"], str) or not _ROOT_PUB_RE.match(f["kem_pub"]):
        raise ArmorError("v2 passkey factor kem_pub must be 64 lowercase hex chars")
    _b64_field(f, "sealed", length=_RECOVERY_SEAL_LEN, what="passkey.sealed")


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


def _parse_combined_factor(f: dict, index: int) -> None:
    """A combined (MFA) factor: {type, credential_id, kem_pub, kdf, cipher, iv,
    wrap, sealed}. ``wrap`` is the password-wrapped share; ``sealed`` is the
    passkey-sealed share; XOR of the two is the master KEK."""
    if set(f) != {"type", "credential_id", "kem_pub", "kdf", "cipher", "iv", "wrap", "sealed"}:
        raise ArmorError(
            f"v2 combined factor[{index}] must carry exactly "
            "{type, credential_id, kem_pub, kdf, cipher, iv, wrap, sealed}"
        )
    if not isinstance(f["credential_id"], str) or not _CREDENTIAL_ID_RE.match(
        f["credential_id"]
    ):
        raise ArmorError(
            "v2 combined factor credential_id must be base64url (the WebAuthn rawId)"
        )
    if not isinstance(f["kem_pub"], str) or not _ROOT_PUB_RE.match(f["kem_pub"]):
        raise ArmorError("v2 combined factor kem_pub must be 64 lowercase hex chars")
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
            "v2 combined factor kdf must be exactly {name: PBKDF2, hash: "
            "SHA-256, iterations, salt} with sane iterations"
        )
    if f["cipher"] != "AES-256-GCM":
        raise ArmorError("v2 combined factor cipher must be AES-256-GCM")
    _b64_field(kdf, "salt", length=_SALT_LEN, what="combined.kdf.salt")
    _b64_field(f, "iv", length=_IV_LEN, what="combined.iv")
    _b64_field(f, "wrap", length=_WRAP_LEN, what="combined.wrap")
    _b64_field(f, "sealed", length=_RECOVERY_SEAL_LEN, what="combined.sealed")


# Factor-type dispatch is TOTAL BY CONSTRUCTION: the registry IS the parser
# table, so a type cannot be "known" without a strict parser. Adding a factor
# type (recovery, passkey, combined) means adding its parser here — there is no
# path where a registered type clears the membership check yet hits no
# field-closure, which would reopen I1 at the exact growth point this envelope
# exists for.
_FACTOR_PARSERS = {
    "password": _parse_password_factor,
    "recovery": _parse_recovery_factor,
    "passkey": _parse_passkey_factor,
    "combined": _parse_combined_factor,
}
#: Closed registry of factor types (unit 1); derived so it cannot drift from the
#: parsers. recovery/passkey add a (type -> strict parser) entry above.
_KNOWN_FACTOR_TYPES = frozenset(_FACTOR_PARSERS)


def parse_armor(armor: str) -> dict:
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
    if data["v"] != ARMOR_VERSION:
        raise ArmorError(f"parse_armor got version {data['v']!r}, expected 2")
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
        # Singular types (password, recovery) dedupe on type; passkey and
        # combined are plural, one per credential, so they dedupe on
        # (type, credential_id).
        plural = ftype in ("passkey", "combined")
        dedup_key = (ftype, f.get("credential_id")) if plural else ftype
        if dedup_key in seen:
            raise ArmorError(
                f"v2 has a duplicate factor {dedup_key!r}"
                if plural
                else f"v2 has a duplicate factor type {ftype!r}"
            )
        seen.add(dedup_key)
        parser(f, i)  # total dispatch — a known type always has a strict parser
    return data


def decrypt_root_key(armor: str, passphrase: str) -> KeyPair:
    """Open a v2 armor with *passphrase*: unwrap the master KEK from the
    password factor, then unseal the seed under the master KEK."""
    data = parse_armor(armor)
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

    data = parse_armor(armor)
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
    return _emit_v2(parse_armor(_emit_v2(data)))


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
    data = parse_armor(armor)
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
    return _emit_v2(parse_armor(_emit_v2(data)))


def armor_factor_types(armor: str) -> list:
    """Which locks this armor carries, in declaration order."""
    return [f["type"] for f in parse_armor(armor)["factors"]]


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

    data = parse_armor(armor)
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


def add_passkey_factor(
    armor: str, passphrase: str, credential_id: str, passkey_kem_pub: str
) -> str:
    """Promote a passkey to a full factor of this armor, knowing only its
    PUBLIC half.

    ``passkey_kem_pub`` is the passkey's provisioning key — the public half a
    passkey publishes in its enrollment statement, derived from its PRF output
    under :data:`PASSKEY_ARMOR_PURPOSE`. Sealing the master KEK to it makes a
    fresh PRF eval on that device able to open this armor ALONE later. The PRF
    private half never enters this process; only the owner, who can already open
    the armor with *passphrase*, can add a lock, and every existing factor keeps
    working. Plural by design: one passkey factor per credential.
    """
    if not isinstance(credential_id, str) or not _CREDENTIAL_ID_RE.match(credential_id):
        raise ArmorError("credential_id must be base64url (the WebAuthn rawId)")
    if not isinstance(passkey_kem_pub, str) or not _ROOT_PUB_RE.match(passkey_kem_pub):
        raise ArmorError("passkey_kem_pub must be 64 lowercase hex chars")
    from .sealing import seal

    data = parse_armor(armor)
    if any(
        f["type"] == "passkey" and f["credential_id"] == credential_id
        for f in data["factors"]
    ):
        raise ArmorError(
            "this armor already carries a factor for that passkey; replacing one "
            "is a rotation, not an addition"
        )
    master_kek = _v2_master_kek(data, passphrase)
    previous_factors = list(data["factors"])
    sealed = seal(master_kek, passkey_kem_pub, PASSKEY_ARMOR_PURPOSE)
    if len(sealed) != _RECOVERY_SEAL_LEN:
        raise ArmorError("sealed passkey wrap is not the expected length")
    data["factors"] = [
        *data["factors"],
        {
            "type": "passkey",
            "credential_id": credential_id,
            "kem_pub": passkey_kem_pub,
            "sealed": base64.b64encode(sealed).decode("ascii"),
        },
    ]
    _reseal_to_factor_set(data, master_kek, previous_factors)
    return _emit_v2(parse_armor(_emit_v2(data)))


def remove_passkey_factor(armor: str, passphrase: str, credential_id: str) -> str:
    """Demote one passkey — drop the factor for *credential_id*.

    Authorised by the passphrase, like :func:`remove_factor`: changing the set
    re-seals the seed, which needs the master KEK. The last lock can never be
    removed. As with any factor removal, this does not reach copies of the file
    that already exist — a real demotion of a compromised device pairs with a
    root rotation (:mod:`tools.network.idkit.root_rotation`).
    """
    data = parse_armor(armor)
    match = [
        f
        for f in data["factors"]
        if f["type"] == "passkey" and f["credential_id"] == credential_id
    ]
    if not match:
        raise ArmorError("this armor carries no passkey factor for that credential")
    remaining = [
        f
        for f in data["factors"]
        if not (f["type"] == "passkey" and f["credential_id"] == credential_id)
    ]
    if not remaining:
        raise ArmorError(
            "refusing to remove the last factor: an armor nothing can open is "
            "a destroyed identity, not a hardened one"
        )
    master_kek = _v2_master_kek(data, passphrase)
    previous_factors = list(data["factors"])
    data["factors"] = remaining
    _reseal_to_factor_set(data, master_kek, previous_factors)
    return _emit_v2(parse_armor(_emit_v2(data)))


def decrypt_root_key_with_passkey(armor: str, prf_output: bytes) -> KeyPair:
    """Open a v2 armor with a passkey's PRF output ALONE.

    The PRF output re-derives the passkey's encapsulation private key under
    :data:`PASSKEY_ARMOR_PURPOSE`; its public half selects the matching factor,
    whose seal yields the master KEK, which unseals the identity. This is a
    single-factor open — a passkey-only unlock — and is refused when no passkey
    factor's public half matches this output.
    """
    from .sealing import derive_encapsulation_keypair
    from .sealing import open as seal_open

    data = parse_armor(armor)
    root_pub = data["root_pub"]
    private_hex, public_hex = derive_encapsulation_keypair(
        bytes(prf_output), PASSKEY_ARMOR_PURPOSE
    )
    factor = next(
        (
            f
            for f in data["factors"]
            if f["type"] == "passkey" and f["kem_pub"] == public_hex
        ),
        None,
    )
    if factor is None:
        raise ArmorPassphraseError(
            "no passkey factor on this armor opens with that ceremony's output"
        )
    try:
        sealed = base64.b64decode(factor["sealed"], validate=True)
        seal_iv = base64.b64decode(data["kek_seal"]["iv"], validate=True)
        seal_ct = base64.b64decode(data["kek_seal"]["ct"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArmorError(f"v2 armor fields do not base64-decode: {exc}") from exc
    try:
        master_kek = seal_open(sealed, private_hex, PASSKEY_ARMOR_PURPOSE)
    except Exception as exc:
        raise ArmorPassphraseError(
            "the passkey factor does not open with that ceremony's output"
        ) from exc
    return _open_seed_under_master_kek(
        master_kek, seal_iv, seal_ct, root_pub, data["factors"]
    )


def _xor32(a: bytes, b: bytes) -> bytes:
    if len(a) != _MASTER_KEK_LEN or len(b) != _MASTER_KEK_LEN:
        raise ArmorError("combined-factor shares must both be 32 bytes")
    return bytes(x ^ y for x, y in zip(a, b))


def _build_combined_factor(
    root_pub: str,
    master_kek: bytes,
    passphrase: str,
    credential_id: str,
    passkey_kem_pub: str,
    iterations: int,
) -> dict:
    """Assemble ONE combined (MFA) factor from a known master KEK.

    The master KEK is 2-of-2 XOR-split into ``share_pw`` (random) and
    ``share_pk = master_kek XOR share_pw``. ``share_pw`` is AES-GCM-wrapped
    under ``KDF(passphrase)``; ``share_pk`` is sealed to the passkey's PUBLIC
    provisioning key. Reconstructing the master KEK needs BOTH — neither the
    password nor the passkey alone yields it. Only public material and the
    passphrase are needed here; the passkey PRF private half never enters.
    """
    from .sealing import seal

    if not isinstance(credential_id, str) or not _CREDENTIAL_ID_RE.match(credential_id):
        raise ArmorError("credential_id must be base64url (the WebAuthn rawId)")
    if not isinstance(passkey_kem_pub, str) or not _ROOT_PUB_RE.match(passkey_kem_pub):
        raise ArmorError("passkey_kem_pub must be 64 lowercase hex chars")
    share_pw = os.urandom(_MASTER_KEK_LEN)
    share_pk = _xor32(master_kek, share_pw)
    salt = os.urandom(_SALT_LEN)
    pw_key = _derive_key(passphrase, salt, iterations)
    wrap_iv = os.urandom(_IV_LEN)
    wrap = AESGCM(pw_key).encrypt(
        wrap_iv, share_pw, _v2_factor_aad(root_pub, "combined")
    )
    # The passkey share is sealed to the SAME provisioning key and label as a
    # standalone passkey factor (the published kem_pub is derived under that
    # label, and there is no PRF at build time to derive another). What is
    # sealed here is only a SHARE of the master KEK, never the KEK; the factor
    # SET commitment (the seed seal's AAD) is what binds this share to the
    # combined slot, so it cannot be spliced into a standalone passkey factor.
    sealed = seal(share_pk, passkey_kem_pub, PASSKEY_ARMOR_PURPOSE)
    if len(sealed) != _RECOVERY_SEAL_LEN:
        raise ArmorError("sealed combined-factor share is not the expected length")
    return {
        "type": "combined",
        "credential_id": credential_id,
        "kem_pub": passkey_kem_pub,
        "kdf": {
            "name": "PBKDF2",
            "hash": "SHA-256",
            "iterations": iterations,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "cipher": "AES-256-GCM",
        "iv": base64.b64encode(wrap_iv).decode("ascii"),
        "wrap": base64.b64encode(wrap).decode("ascii"),
        "sealed": base64.b64encode(sealed).decode("ascii"),
    }


def encrypt_root_key_combined(
    key: KeyPair,
    passphrase: str,
    credential_id: str,
    passkey_kem_pub: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
) -> str:
    """Armor *key* under a SINGLE combined (MFA) factor from nothing.

    The founding path for a fresh identity that is multi-factor from birth:
    there is no intermediate password-only or passkey-only state and no
    standalone factor is ever written, so the armor requires BOTH the password
    and a passkey PRF from its first byte. Mirrors :func:`encrypt_root_key`,
    swapping the lone password factor for a lone combined factor.
    """
    if not (_MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS):
        raise ArmorError(
            f"iterations must be in [{_MIN_ITERATIONS}, {_MAX_ITERATIONS}]"
        )
    root_pub = key.public_hex
    seed = bytes.fromhex(key.private_hex)
    master_kek = os.urandom(_MASTER_KEK_LEN)
    factors = [
        _build_combined_factor(
            root_pub, master_kek, passphrase, credential_id, passkey_kem_pub, iterations
        )
    ]
    seal_iv = os.urandom(_IV_LEN)
    seal_ct = AESGCM(master_kek).encrypt(
        seal_iv, seed, _v2_seal_aad(root_pub, factors)
    )
    body = canonical_json(
        {
            "v": ARMOR_VERSION,
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


def enable_mfa(
    armor: str,
    passphrase: str,
    credential_id: str,
    passkey_kem_pub: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
) -> str:
    """Combine an existing password + a passkey into ONE MFA factor and CLEAR
    the individual factors.

    The upgrade path: you already hold both materials (the passphrase opens the
    current armor; ``passkey_kem_pub`` is the device's published provisioning
    key). This writes a combined factor requiring both together and then
    DELETES every standalone ``password`` and ``passkey`` factor — because a
    standalone opener sitting beside the pair would defeat the point of the
    pair. The recovery factor (break-glass) is deliberately preserved: MFA
    hardens your daily factors, it does not throw away the code you reach for
    when a device is gone. After this, neither the password nor the passkey
    ALONE opens the armor.
    """
    if not (_MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS):
        raise ArmorError(
            f"iterations must be in [{_MIN_ITERATIONS}, {_MAX_ITERATIONS}]"
        )
    if not isinstance(passkey_kem_pub, str) or not _ROOT_PUB_RE.match(passkey_kem_pub):
        raise ArmorError("passkey_kem_pub must be 64 lowercase hex chars")
    data = parse_armor(armor)
    root_pub = data["root_pub"]
    if any(f["type"] == "combined" for f in data["factors"]):
        raise ArmorError(
            "this armor is already multi-factor; enabling MFA again is a "
            "no-op, not an addition"
        )
    # Authorise with the password (which also proves you hold the standalone
    # factor being combined), and recover the master KEK to re-split it.
    master_kek = _v2_master_kek(data, passphrase)
    previous_factors = list(data["factors"])
    combined = _build_combined_factor(
        root_pub, master_kek, passphrase, credential_id, passkey_kem_pub, iterations
    )
    # Clear the individual openers; keep everything else (e.g. recovery).
    data["factors"] = [
        combined,
        *(f for f in data["factors"] if f["type"] not in ("password", "passkey")),
    ]
    _reseal_to_factor_set(data, master_kek, previous_factors)
    return _emit_v2(parse_armor(_emit_v2(data)))


def decrypt_root_key_with_combined(
    armor: str, passphrase: str, prf_output: bytes
) -> KeyPair:
    """Open a v2 armor with a combined (MFA) factor: BOTH password AND passkey.

    The passphrase unwraps ``share_pw``; the passkey PRF re-derives the
    provisioning private key (selecting the matching combined factor by its
    public half) and unseals ``share_pk``; their XOR is the master KEK, which
    unseals the identity. Supplying only one half fails — that is the whole
    guarantee. Refused when no combined factor's public half matches the PRF
    output.
    """
    from .sealing import derive_encapsulation_keypair
    from .sealing import open as seal_open

    data = parse_armor(armor)
    root_pub = data["root_pub"]
    private_hex, public_hex = derive_encapsulation_keypair(
        bytes(prf_output), PASSKEY_ARMOR_PURPOSE
    )
    factor = next(
        (
            f
            for f in data["factors"]
            if f["type"] == "combined" and f["kem_pub"] == public_hex
        ),
        None,
    )
    if factor is None:
        raise ArmorPassphraseError(
            "no combined factor on this armor pairs with that ceremony's output"
        )
    try:
        salt = base64.b64decode(factor["kdf"]["salt"], validate=True)
        wrap_iv = base64.b64decode(factor["iv"], validate=True)
        wrap = base64.b64decode(factor["wrap"], validate=True)
        sealed = base64.b64decode(factor["sealed"], validate=True)
        seal_iv = base64.b64decode(data["kek_seal"]["iv"], validate=True)
        seal_ct = base64.b64decode(data["kek_seal"]["ct"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArmorError(f"v2 armor fields do not base64-decode: {exc}") from exc
    pw_key = _derive_key(passphrase, salt, factor["kdf"]["iterations"])
    try:
        share_pw = AESGCM(pw_key).decrypt(
            wrap_iv, wrap, _v2_factor_aad(root_pub, "combined")
        )
    except InvalidTag as exc:
        raise ArmorPassphraseError(
            "the combined factor does not open with that passphrase"
        ) from exc
    try:
        share_pk = seal_open(sealed, private_hex, PASSKEY_ARMOR_PURPOSE)
    except Exception as exc:
        raise ArmorPassphraseError(
            "the combined factor does not open with that ceremony's output"
        ) from exc
    master_kek = _xor32(share_pw, share_pk)
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

    data = parse_armor(armor)
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
    return _emit_v2(parse_armor(_emit_v2(data)))


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


def armor_version(armor: str) -> int:
    """The declared version of *armor*, else :class:`ArmorError`.

    There is one armor format. This stays as a named check rather than an
    inline comparison because the parse it performs is the strict one: a blob
    that does not carry exactly ``v: 2`` is refused here, before anything
    tries to read it.
    """
    v = _armor_body(armor).get("v")
    if v != ARMOR_VERSION:
        raise ArmorError(f"unsupported armor version: {v!r}")
    return v


def armor_root_pub(armor: str) -> str:
    """The bound ``root_pub`` of an armor, WITHOUT decrypting it.

    For the call sites that need only the identity, not the key. Strict-parses,
    so a malformed blob is still refused.
    """
    return parse_armor(armor)["root_pub"]


