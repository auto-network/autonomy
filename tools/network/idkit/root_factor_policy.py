"""Factor-generic policy protection for the personal Ed25519 root.

The legacy v2 armor represents root authority as one of four physical shapes:
one password, one or more passkeys, their OR, or one password paired with one
specific passkey.  That shape cannot express the product policy we actually
need::

    OR(active password factors) AND OR(active passkey factors)

This module is the versioned cryptographic core for that policy.  It deliberately
does not know about dashboard rows or UI operations.  It accepts a frozen list
of public factor recipients and a canonical AND/OR expression, distributes the
32-byte root seed through that expression, and signs the complete generation
with the root.  The signature is verified *before* any factor material is used,
so an open-write store cannot substitute its own policy or recipient.

Construction:

* OR gives every child the same 32-byte node secret.
* AND XOR-splits the node secret across every child.
* A leaf HPKE-seals its share to every X25519 recipient of that logical factor.
  Passwords have one recipient; a synced passkey has one recipient per device
  PRF slot while remaining one credential-level policy member.

Every factor id may occur once in an expression.  That independence rule keeps
the elementary XOR construction honest; expressions that repeat a factor must
first be normalized (``A AND (B OR C)``, never ``(A AND B) OR (A AND C)``).

Password factors are normalized to the same public-recipient interface as
passkeys: the password encrypts a fresh random factor seed; that seed derives a
stable X25519 recipient.  Adding or rewrapping another factor therefore needs
the current policy once plus the *new* factor, never any other factor secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import textwrap
from copy import deepcopy
from typing import Iterable, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .canonical import canonical_json
from .keys import KeyPair, verify_signature
from .sealing import (
    derive_encapsulation_keypair,
    open as open_sealed,
    seal,
)


POLICY_VERSION = 1
POLICY_SIGNATURE_DOMAIN = b"autonomy.identity.root-factor-policy.v1\n"
# Deliberately distinct from ``autonomy/vault-factor/v1``.  One physical
# password/passkey gesture may be enrolled for root authority, a custom vault
# class, or both, but those memberships must not reuse a cryptographic
# recipient.  Legacy v2 armor used the vault-factor label; migration is already
# a root-authorized re-arm and derives this root-only recipient at that point.
FACTOR_RECIPIENT_PURPOSE = "autonomy/root-factor-recipient/v1"
FACTOR_ACCESS_DERIVE_INFO = b"autonomy.identity.factor-access.v1\n"
PASSWORD_FACTOR_AAD = b"autonomy.identity.password-factor.v1\n"
WRAP_PURPOSE_PREFIX = "autonomy/root-policy-wrap/v1"
ARMOR_VERSION = 3
ARMOR_BEGIN = "-----BEGIN AUTONOMY NETWORK ROOT KEY-----"
ARMOR_END = "-----END AUTONOMY NETWORK ROOT KEY-----"

PASSWORD_KDF_MIN = 10_000
PASSWORD_KDF_MAX = 100_000_000
PASSWORD_KDF_DEFAULT = 600_000
MAX_FACTORS = 32
MAX_POLICY_DEPTH = 16

_FACTOR_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HEX_32_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_CREDENTIAL_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,256}\Z")
_UTC_TS_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


class RootFactorPolicyError(ValueError):
    """A root-factor policy or its cryptographic material is invalid."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: object, *, length: int, what: str) -> bytes:
    if not isinstance(value, str):
        raise RootFactorPolicyError(f"{what} must be canonical base64")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RootFactorPolicyError(f"{what} must be canonical base64") from exc
    if len(raw) != length or _b64(raw) != value:
        raise RootFactorPolicyError(f"{what} must decode to exactly {length} bytes")
    return raw


def _factor_id(value: object) -> str:
    if not isinstance(value, str) or not _FACTOR_ID_RE.fullmatch(value):
        raise RootFactorPolicyError(
            "factor_id must be 1-128 ASCII letters, digits, '.', '_', ':', or '-'"
        )
    return value


def _public_key(value: object, what: str) -> str:
    if not isinstance(value, str) or not _HEX_32_RE.fullmatch(value):
        raise RootFactorPolicyError(f"{what} must be 64 lowercase hex characters")
    return value


def _password_aad(
    root_pub: str,
    factor_id: str,
    recipient_public_key: str,
    access_public_key: str,
) -> bytes:
    return PASSWORD_FACTOR_AAD + canonical_json({
        "access_public_key": access_public_key,
        "factor_id": factor_id,
        "recipient_public_key": recipient_public_key,
        "root_pub": root_pub,
    })


def factor_access_keypair(seed: bytes | bytearray) -> KeyPair:
    """Derive the dashboard-access signer for one normalized factor seed.

    This key is deliberately independent from both the factor's X25519 root
    recipient and the personal root. Possession may therefore authorize a
    dashboard session without silently releasing root authority.
    """
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
        raise RootFactorPolicyError("factor seed must be exactly 32 bytes")
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=FACTOR_ACCESS_DERIVE_INFO,
    ).derive(bytes(seed))
    return KeyPair(Ed25519PrivateKey.from_private_bytes(raw))


def create_password_factor(
    root_pub: str,
    factor_id: str,
    password: str,
    *,
    iterations: int = PASSWORD_KDF_DEFAULT,
) -> tuple[dict, bytearray]:
    """Create one password-protected public recipient.

    Returns the strict public/encrypted descriptor plus the mutable 32-byte
    factor seed.  The caller uses the seed to open or compile a policy and must
    zero it when done.  The seed, not the password, is the factor's stable
    cryptographic identity.
    """
    root_pub = _public_key(root_pub, "root_pub")
    factor_id = _factor_id(factor_id)
    if not isinstance(password, str) or not password:
        raise RootFactorPolicyError("password must be a non-empty string")
    if (
        not isinstance(iterations, int)
        or isinstance(iterations, bool)
        or not PASSWORD_KDF_MIN <= iterations <= PASSWORD_KDF_MAX
    ):
        raise RootFactorPolicyError("password KDF iterations are out of range")

    seed = bytearray(os.urandom(32))
    _, recipient_pub = derive_encapsulation_keypair(seed, FACTOR_RECIPIENT_PURPOSE)
    access_pub = factor_access_keypair(seed).public_hex
    salt = os.urandom(16)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations,
    ).derive(password.encode("utf-8"))
    iv = os.urandom(12)
    wrapped = AESGCM(key).encrypt(
        iv,
        bytes(seed),
        _password_aad(root_pub, factor_id, recipient_pub, access_pub),
    )
    return {
        "factor_id": factor_id,
        "type": "password",
        "recipient_public_key": recipient_pub,
        "access_public_key": access_pub,
        "protector": {
            "kdf": {
                "name": "PBKDF2",
                "hash": "SHA-256",
                "iterations": iterations,
                "salt": _b64(salt),
            },
            "cipher": "AES-256-GCM",
            "iv": _b64(iv),
            "wrapped_seed": _b64(wrapped),
        },
    }, seed


def passkey_factor(
    factor_id: str,
    credential_id: str,
    recipient_public_key: str | None,
    *,
    recipient_label: str = "Device",
    recipient_created_at: str = "1970-01-01T00:00:00Z",
) -> dict:
    factor_id = _factor_id(factor_id)
    if not isinstance(credential_id, str) or not _CREDENTIAL_ID_RE.fullmatch(
        credential_id
    ):
        raise RootFactorPolicyError("credential_id must be canonical base64url")
    recipients = [] if recipient_public_key is None else [{
        "recipient_public_key": _public_key(
            recipient_public_key, "passkey recipient public key",
        ),
        "label": _recipient_label(recipient_label),
        "created_at": _recipient_created_at(recipient_created_at),
    }]
    return {
        "factor_id": factor_id,
        "type": "passkey",
        "credential_id": credential_id,
        "recipients": recipients,
    }


def _recipient_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise RootFactorPolicyError("passkey recipient label must be 1-120 characters")
    return value.strip()


def _recipient_created_at(value: object) -> str:
    if not isinstance(value, str) or not _UTC_TS_RE.fullmatch(value):
        raise RootFactorPolicyError("passkey recipient created_at must be UTC ISO-8601")
    return value


def parse_passkey_recipient(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "recipient_public_key", "label", "created_at",
    }:
        raise RootFactorPolicyError(
            "passkey recipient must carry recipient_public_key, label, and created_at"
        )
    return {
        "recipient_public_key": _public_key(
            value.get("recipient_public_key"), "passkey recipient public key",
        ),
        "label": _recipient_label(value.get("label")),
        "created_at": _recipient_created_at(value.get("created_at")),
    }


def factor_recipient_public_keys(factor: Mapping) -> list[str]:
    if factor.get("type") == "password":
        return [factor["recipient_public_key"]]
    return [row["recipient_public_key"] for row in factor.get("recipients", [])]


def parse_factor(value: object, *, root_pub: str) -> dict:
    if not isinstance(value, dict):
        raise RootFactorPolicyError("factor descriptor must be an object")
    factor_id = _factor_id(value.get("factor_id"))
    factor_type = value.get("type")
    if factor_type == "passkey":
        if set(value) != {
            "factor_id", "type", "credential_id", "recipients",
        }:
            raise RootFactorPolicyError(
                "passkey factor must carry exactly factor_id, type, "
                "credential_id, and recipients"
            )
        credential_id = value.get("credential_id")
        if not isinstance(credential_id, str) or not _CREDENTIAL_ID_RE.fullmatch(
            credential_id
        ):
            raise RootFactorPolicyError("credential_id must be canonical base64url")
        recipients = value.get("recipients")
        if not isinstance(recipients, list) or len(recipients) > MAX_FACTORS:
            raise RootFactorPolicyError("passkey recipients must be a bounded array")
        parsed_recipients = [parse_passkey_recipient(row) for row in recipients]
        parsed_recipients.sort(key=lambda row: row["recipient_public_key"])
        public_keys = [row["recipient_public_key"] for row in parsed_recipients]
        if len(set(public_keys)) != len(public_keys):
            raise RootFactorPolicyError("passkey recipient public keys must be unique")
        return {
            "factor_id": factor_id,
            "type": "passkey",
            "credential_id": credential_id,
            "recipients": parsed_recipients,
        }
    if factor_type != "password":
        raise RootFactorPolicyError("root factor type must be password or passkey")
    recipient_pub = _public_key(
        value.get("recipient_public_key"), "factor recipient public key"
    )
    if set(value) != {
        "factor_id", "type", "recipient_public_key", "access_public_key", "protector",
    }:
        raise RootFactorPolicyError(
            "password factor must carry exactly factor_id, type, "
            "recipient_public_key, access_public_key, and protector"
        )
    access_pub = _public_key(value.get("access_public_key"), "factor access public key")
    protector = value.get("protector")
    if not isinstance(protector, dict) or set(protector) != {
        "kdf", "cipher", "iv", "wrapped_seed",
    } or protector.get("cipher") != "AES-256-GCM":
        raise RootFactorPolicyError("password factor protector is malformed")
    kdf = protector.get("kdf")
    if not isinstance(kdf, dict) or set(kdf) != {
        "name", "hash", "iterations", "salt",
    } or kdf.get("name") != "PBKDF2" or kdf.get("hash") != "SHA-256":
        raise RootFactorPolicyError("password factor KDF is malformed")
    iterations = kdf.get("iterations")
    if (
        not isinstance(iterations, int)
        or isinstance(iterations, bool)
        or not PASSWORD_KDF_MIN <= iterations <= PASSWORD_KDF_MAX
    ):
        raise RootFactorPolicyError("password factor KDF iterations are out of range")
    _unb64(kdf.get("salt"), length=16, what="password factor salt")
    _unb64(protector.get("iv"), length=12, what="password factor iv")
    _unb64(
        protector.get("wrapped_seed"), length=48, what="password wrapped seed"
    )
    return deepcopy(value)


def open_password_factor(root_pub: str, factor: Mapping, password: str) -> bytearray:
    factor = parse_factor(dict(factor), root_pub=root_pub)
    if factor["type"] != "password":
        raise RootFactorPolicyError("the selected factor is not a password")
    if not isinstance(password, str) or not password:
        raise RootFactorPolicyError("password must be a non-empty string")
    protector = factor["protector"]
    kdf = protector["kdf"]
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_unb64(kdf["salt"], length=16, what="password factor salt"),
        iterations=kdf["iterations"],
    ).derive(password.encode("utf-8"))
    try:
        seed = bytearray(AESGCM(key).decrypt(
            _unb64(protector["iv"], length=12, what="password factor iv"),
            _unb64(
                protector["wrapped_seed"], length=48, what="password wrapped seed"
            ),
            _password_aad(
                root_pub,
                factor["factor_id"],
                factor["recipient_public_key"],
                factor["access_public_key"],
            ),
        ))
    except InvalidTag as exc:
        raise RootFactorPolicyError("password factor did not open") from exc
    _, recipient_pub = derive_encapsulation_keypair(seed, FACTOR_RECIPIENT_PURPOSE)
    access_pub = factor_access_keypair(seed).public_hex
    if (
        recipient_pub != factor["recipient_public_key"]
        or access_pub != factor["access_public_key"]
    ):
        seed[:] = b"\x00" * len(seed)
        raise RootFactorPolicyError("password factor seed does not match its public keys")
    return seed


def factor_leaf(factor_id: str) -> dict:
    return {"op": "factor", "factor_id": _factor_id(factor_id)}


def _canonical_expression(value: object, leaves: list[str], *, depth: int) -> dict:
    if depth > MAX_POLICY_DEPTH:
        raise RootFactorPolicyError(
            f"root policy exceeds the maximum depth of {MAX_POLICY_DEPTH}"
        )
    if not isinstance(value, dict) or "op" not in value:
        raise RootFactorPolicyError("policy node must be an object with an op")
    op = value.get("op")
    if op == "factor":
        if set(value) != {"op", "factor_id"}:
            raise RootFactorPolicyError("factor node must carry exactly op and factor_id")
        factor_id = _factor_id(value.get("factor_id"))
        if factor_id in leaves:
            raise RootFactorPolicyError(
                f"factor {factor_id!r} occurs more than once in the policy"
            )
        leaves.append(factor_id)
        return factor_leaf(factor_id)
    if op not in {"and", "or"} or set(value) != {"op", "children"}:
        raise RootFactorPolicyError(
            "policy node must be factor, or an and/or carrying only children"
        )
    children = value.get("children")
    if not isinstance(children, list) or len(children) < 2:
        raise RootFactorPolicyError(f"{op} node must have at least two children")
    parsed = [
        _canonical_expression(child, leaves, depth=depth + 1)
        for child in children
    ]
    parsed.sort(key=canonical_json)
    return {"op": op, "children": parsed}


def canonical_expression(value: object) -> dict:
    leaves: list[str] = []
    parsed = _canonical_expression(value, leaves, depth=1)
    if len(leaves) > MAX_FACTORS:
        raise RootFactorPolicyError(
            f"root policy exceeds the maximum of {MAX_FACTORS} factor leaves"
        )
    return parsed


def policy_factor_ids(policy: object) -> tuple[str, ...]:
    parsed = canonical_expression(policy)
    out: list[str] = []

    def walk(node: dict) -> None:
        if node["op"] == "factor":
            out.append(node["factor_id"])
        else:
            for child in node["children"]:
                walk(child)

    walk(parsed)
    return tuple(out)


def policy_satisfied(policy: object, supplied_factor_ids: Iterable[str]) -> bool:
    node = canonical_expression(policy)
    supplied = set(supplied_factor_ids)

    def evaluate(current: dict) -> bool:
        if current["op"] == "factor":
            return current["factor_id"] in supplied
        values = [evaluate(child) for child in current["children"]]
        return all(values) if current["op"] == "and" else any(values)

    return evaluate(node)


def factor_roles(policy: object, factors: Iterable[Mapping], access: Iterable[str]) -> dict:
    parsed = canonical_expression(policy)
    factor_map = {str(f["factor_id"]): f for f in factors}
    members = set(policy_factor_ids(parsed))
    access_set = set(access)
    roles = {}
    for factor_id in factor_map:
        if factor_id in members:
            root_role = (
                "individual"
                if policy_satisfied(parsed, {factor_id})
                else "mfa-member"
            )
        else:
            root_role = "none"
        roles[factor_id] = {
            "access": "enabled" if factor_id in access_set else "disabled",
            "root_role": root_role,
        }
    return roles


def grouped_mfa_policy(
    factors: Iterable[Mapping],
    *,
    password_ids: Iterable[str] | None = None,
    passkey_ids: Iterable[str] | None = None,
) -> dict:
    rows = [dict(factor) for factor in factors]
    by_type = {
        kind: [f["factor_id"] for f in rows if f.get("type") == kind]
        for kind in ("password", "passkey")
    }
    password_members = list(password_ids) if password_ids is not None else by_type["password"]
    passkey_members = list(passkey_ids) if passkey_ids is not None else by_type["passkey"]
    if not password_members or not passkey_members:
        raise RootFactorPolicyError(
            "grouped MFA requires at least one password and one passkey"
        )

    def group(ids: list[str]) -> dict:
        leaves = [factor_leaf(value) for value in ids]
        return leaves[0] if len(leaves) == 1 else {"op": "or", "children": leaves}

    return canonical_expression({
        "op": "and",
        "children": [group(password_members), group(passkey_members)],
    })


def validate_state(
    factors: Iterable[Mapping],
    access: Iterable[str],
    policy: object,
    *,
    root_pub: str,
) -> dict:
    root_pub = _public_key(root_pub, "root_pub")
    parsed_factors = [parse_factor(dict(value), root_pub=root_pub) for value in factors]
    if len(parsed_factors) > MAX_FACTORS:
        raise RootFactorPolicyError(
            f"factor inventory exceeds the maximum of {MAX_FACTORS} factors"
        )
    ids = [factor["factor_id"] for factor in parsed_factors]
    if len(set(ids)) != len(ids):
        raise RootFactorPolicyError("factor ids must be unique")
    credential_ids = [
        factor["credential_id"] for factor in parsed_factors
        if factor["type"] == "passkey"
    ]
    if len(set(credential_ids)) != len(credential_ids):
        raise RootFactorPolicyError(
            "one passkey credential must be represented by one logical factor"
        )
    parsed_factors.sort(key=lambda factor: factor["factor_id"])
    factor_map = {factor["factor_id"]: factor for factor in parsed_factors}
    parsed_policy = canonical_expression(policy)
    members = set(policy_factor_ids(parsed_policy))
    missing = members - set(factor_map)
    if missing:
        raise RootFactorPolicyError(
            f"root policy names unknown factors: {sorted(missing)}"
        )
    incapable = sorted(
        factor_id for factor_id in members
        if not factor_recipient_public_keys(factor_map[factor_id])
    )
    if incapable:
        raise RootFactorPolicyError(
            "root policy includes factors without a derivable recipient: "
            f"{incapable}"
        )
    recipients = [
        recipient
        for factor_id in members
        for recipient in factor_recipient_public_keys(factor_map[factor_id])
    ]
    if len(set(recipients)) != len(recipients):
        raise RootFactorPolicyError(
            "root policy repeats one cryptographic recipient under multiple factor ids"
        )
    access_ids = sorted(set(access))
    unknown_access = set(access_ids) - set(factor_map)
    if unknown_access:
        raise RootFactorPolicyError(
            f"dashboard access names unknown factors: {sorted(unknown_access)}"
        )
    # A passkey without a PRF-derived recipient may remain in the inventory and
    # in dashboard access, but the incapable-member check above keeps it out of
    # every root policy.
    if not access_ids and not members:
        raise RootFactorPolicyError("the resulting state has no dashboard unlock route")
    return {
        "factors": parsed_factors,
        "access": access_ids,
        "policy": parsed_policy,
        "roles": factor_roles(parsed_policy, parsed_factors, access_ids),
    }


def project_operations(
    current: Mapping,
    operations: Iterable[Mapping],
) -> dict:
    """Apply a staged operation batch and validate only its final state.

    UI batches are intentionally allowed to pass through transiently invalid
    states—removing the last password and adding its replacement is legal when
    committed together.  Structural errors in an operation fail immediately;
    reachability, membership and capability are checked exactly once after all
    operations have been projected.
    """
    if not isinstance(current, Mapping):
        raise RootFactorPolicyError("current factor-policy state must be an object")
    generation = current.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise RootFactorPolicyError("current generation must be a positive integer")
    root_pub = _public_key(current.get("root_pub"), "root_pub")
    state = validate_state(
        current.get("factors") if isinstance(current.get("factors"), list) else [],
        current.get("access") if isinstance(current.get("access"), list) else [],
        current.get("policy"),
        root_pub=root_pub,
    )
    factors = {factor["factor_id"]: factor for factor in state["factors"]}
    access = set(state["access"])
    policy = state["policy"]
    # The recovery slot rides through unchanged unless a set_recovery op touches
    # it — it is outside the policy, so ordinary factor ops carry it forward.
    recovery = _parse_recovery(current["recovery"]) if current.get("recovery") is not None else None
    applied = []

    for raw in operations:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("op"), str):
            raise RootFactorPolicyError("each transition operation must name an op")
        operation = dict(raw)
        op = operation["op"]
        if op == "set_recovery":
            # Enroll the recovery slot — ENROLL-ONLY. The recovery field is the
            # cryptographic object (built by add_recovery_slot with the code's
            # public halves). Per the operator ruling the code is NOT replaceable
            # once generated, so this op refuses to clear an existing slot and
            # refuses to overwrite one: "replacement needs the OLD code" holds by
            # construction — there is no clear-then-re-enroll path here. Actual
            # replacement lands later with the timelocked-regeneration substrate.
            if set(operation) != {"op", "recovery"}:
                raise RootFactorPolicyError("set_recovery must carry exactly op and recovery")
            if operation["recovery"] is None:
                raise RootFactorPolicyError(
                    "set_recovery cannot clear the recovery slot; the recovery "
                    "code is not removable once generated"
                )
            if recovery is not None:
                raise RootFactorPolicyError(
                    "a recovery slot already exists and is not replaceable once "
                    "generated; replacement requires the old code (not yet built)"
                )
            recovery = _parse_recovery(operation["recovery"])
            applied.append(op)
            continue
        if op in {"enroll_password", "enroll_passkey"}:
            if set(operation) != {"op", "factor", "access"} \
                    or not isinstance(operation.get("access"), bool):
                raise RootFactorPolicyError(
                    f"{op} must carry exactly op, factor, and boolean access"
                )
            factor = parse_factor(operation.get("factor"), root_pub=root_pub)
            expected_type = op.removeprefix("enroll_")
            if factor["type"] != expected_type:
                raise RootFactorPolicyError(f"{op} received a {factor['type']} factor")
            factor_id = factor["factor_id"]
            if factor_id in factors:
                raise RootFactorPolicyError(f"factor {factor_id!r} is already enrolled")
            factors[factor_id] = factor
            if operation["access"]:
                access.add(factor_id)
            applied.append(op)
            continue
        if op == "change_password":
            if set(operation) != {"op", "factor_id", "factor"}:
                raise RootFactorPolicyError(
                    "change_password must carry exactly op, factor_id, and factor"
                )
            factor_id = _factor_id(operation.get("factor_id"))
            previous = factors.get(factor_id)
            replacement = parse_factor(operation.get("factor"), root_pub=root_pub)
            if previous is None or previous["type"] != "password":
                raise RootFactorPolicyError("change_password targets no password factor")
            if replacement["type"] != "password" or replacement["factor_id"] != factor_id:
                raise RootFactorPolicyError(
                    "changed password must preserve its factor id and type"
                )
            factors[factor_id] = replacement
            applied.append(op)
            continue
        if op == "add_passkey_recipient":
            if set(operation) != {"op", "factor_id", "recipient"}:
                raise RootFactorPolicyError(
                    "add_passkey_recipient must carry op, factor_id, and recipient"
                )
            factor_id = _factor_id(operation.get("factor_id"))
            factor = factors.get(factor_id)
            if factor is None or factor["type"] != "passkey":
                raise RootFactorPolicyError(
                    "add_passkey_recipient targets no passkey factor"
                )
            recipient = parse_passkey_recipient(operation.get("recipient"))
            if recipient["recipient_public_key"] in factor_recipient_public_keys(factor):
                raise RootFactorPolicyError("passkey recipient is already enrolled")
            factor["recipients"].append(recipient)
            factor["recipients"].sort(key=lambda row: row["recipient_public_key"])
            applied.append(op)
            continue
        if op == "remove_passkey_recipient":
            if set(operation) != {"op", "factor_id", "recipient_public_key"}:
                raise RootFactorPolicyError(
                    "remove_passkey_recipient must carry op, factor_id, and "
                    "recipient_public_key"
                )
            factor_id = _factor_id(operation.get("factor_id"))
            factor = factors.get(factor_id)
            if factor is None or factor["type"] != "passkey":
                raise RootFactorPolicyError(
                    "remove_passkey_recipient targets no passkey factor"
                )
            public_key = _public_key(
                operation.get("recipient_public_key"), "passkey recipient public key",
            )
            kept = [
                row for row in factor["recipients"]
                if row["recipient_public_key"] != public_key
            ]
            if len(kept) == len(factor["recipients"]):
                raise RootFactorPolicyError("passkey recipient is not enrolled")
            factor["recipients"] = kept
            applied.append(op)
            continue
        if op == "remove_factor":
            if set(operation) != {"op", "factor_id"}:
                raise RootFactorPolicyError(
                    "remove_factor must carry exactly op and factor_id"
                )
            factor_id = _factor_id(operation.get("factor_id"))
            if factors.pop(factor_id, None) is None:
                raise RootFactorPolicyError(f"factor {factor_id!r} is not enrolled")
            access.discard(factor_id)
            applied.append(op)
            continue
        if op == "set_access":
            if set(operation) != {"op", "factor_id", "enabled"} \
                    or not isinstance(operation.get("enabled"), bool):
                raise RootFactorPolicyError(
                    "set_access must carry exactly op, factor_id, and boolean enabled"
                )
            factor_id = _factor_id(operation.get("factor_id"))
            if factor_id not in factors:
                raise RootFactorPolicyError(f"factor {factor_id!r} is not enrolled")
            if operation["enabled"]:
                access.add(factor_id)
            else:
                access.discard(factor_id)
            applied.append(op)
            continue
        if op == "set_root_policy":
            if set(operation) != {"op", "policy"}:
                raise RootFactorPolicyError(
                    "set_root_policy must carry exactly op and policy"
                )
            # Canonicalize now but defer membership/reachability until the
            # entire batch has been applied.
            policy = canonical_expression(operation.get("policy"))
            applied.append(op)
            continue
        raise RootFactorPolicyError(f"unknown factor-policy operation {op!r}")

    if not applied:
        raise RootFactorPolicyError("transition batch must contain at least one operation")
    projected = validate_state(
        list(factors.values()), sorted(access), policy, root_pub=root_pub,
    )
    return {
        "version": POLICY_VERSION,
        "base_generation": generation,
        "generation": generation + 1,
        "root_pub": root_pub,
        "factors": projected["factors"],
        "access": projected["access"],
        "root_policy": projected["policy"],
        "roles": projected["roles"],
        "recovery": recovery,
        "operations": applied,
        # A staged batch is one root-signed generation regardless of how many
        # rows the operator changed before pressing Apply.
        "change_count": 1,
    }


def _xor(parts: Iterable[bytes]) -> bytearray:
    out = bytearray(32)
    count = 0
    for part in parts:
        if len(part) != 32:
            raise RootFactorPolicyError("policy shares must be exactly 32 bytes")
        count += 1
        for index, byte in enumerate(part):
            out[index] ^= byte
    if not count:
        raise RootFactorPolicyError("cannot XOR an empty share set")
    return out


def _policy_digest(
    generation: int,
    root_pub: str,
    factors: list[dict],
    access: list[str],
    policy: dict,
) -> str:
    return hashlib.sha256(canonical_json({
        "access": access,
        "factors": factors,
        "generation": generation,
        "policy": policy,
        "root_pub": root_pub,
        "v": POLICY_VERSION,
    })).hexdigest()


def _wrap_purpose(digest: str, path: str) -> str:
    return f"{WRAP_PURPOSE_PREFIX}/{digest}/{path}"


def _compile_node(
    node: dict,
    secret: bytes,
    recipients: Mapping[str, list[str]],
    digest: str,
    path: str,
) -> dict:
    if node["op"] == "factor":
        factor_id = node["factor_id"]
        return {
            "op": "factor",
            "factor_id": factor_id,
            "sealed": [{
                "recipient_public_key": public_key,
                "sealed": _b64(seal(
                    secret, public_key, _wrap_purpose(digest, path),
                )),
            } for public_key in recipients[factor_id]],
        }
    if node["op"] == "or":
        return {
            "op": "or",
            "children": [
                _compile_node(child, secret, recipients, digest, f"{path}.{index}")
                for index, child in enumerate(node["children"])
            ],
        }
    shares = [bytearray(os.urandom(32)) for _ in node["children"][:-1]]
    final = _xor([secret, *shares])
    shares.append(final)
    try:
        return {
            "op": "and",
            "children": [
                _compile_node(child, share, recipients, digest, f"{path}.{index}")
                for index, (child, share) in enumerate(zip(node["children"], shares))
            ],
        }
    finally:
        for share in shares:
            share[:] = b"\x00" * len(share)


def _unsigned_envelope(envelope: Mapping) -> dict:
    return {key: deepcopy(value) for key, value in envelope.items() if key != "signature"}


# ── recovery slot (operator ruling 2026-08-27; design graph://fd418706-97e) ──
# The recovery code opens the root ALONE, outside the policy tree — the
# emergency floor. Its field on the envelope seals the ROOT SEED to the code's
# recovery recipient (the same RECOVERY_ARMOR seal the v2 factor uses, so one
# printed code works identically), and carries the code's Ed25519 signing half
# (recovery_pub) for future rotation. Optional: no armor version bump.
_RECOVERY_ARMOR_PURPOSE = "autonomy/recovery-armor/v1"  # mirrors armor.RECOVERY_ARMOR_PURPOSE


def recovery_slot(*, root_seed: bytes, recovery_recipient_pub: str, recovery_pub: str, created_at: str) -> dict:
    """Build one recovery slot dict (seals the root seed to the code recipient).

    HPKE sealing is randomized, so a slot must be built ONCE and used in both
    the set_recovery operation and the candidate armor — hence this helper, so
    the two carry byte-identical `sealed` material.
    """
    if not isinstance(created_at, str) or not _ISO8601_Z_RE.match(created_at):
        raise RootFactorPolicyError("recovery slot created_at must be ISO-8601 Z")
    recipient = _public_key(recovery_recipient_pub, "recovery recipient")
    return {
        "recipient_public_key": recipient,
        "recovery_pub": _public_key(recovery_pub, "recovery_pub"),
        "sealed": _b64(seal(bytes(root_seed), recipient, _RECOVERY_ARMOR_PURPOSE)),
        "created_at": created_at,
    }


def recovery_recipient_public_key(recovery_code: bytes) -> str:
    """The KEM recipient the root seed is sealed to, from the printed code.

    Reuses the v2 derivation verbatim (kek_recovery_seed under the recovery
    armor purpose) so one code opens both v2 and v3 armors.
    """
    from .recovery import derive_recovery_factors

    kek_seed = derive_recovery_factors(recovery_code)["kek_recovery_seed"]
    _private, public = derive_encapsulation_keypair(kek_seed, _RECOVERY_ARMOR_PURPOSE)
    return public


_ISO8601_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _parse_recovery(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "recipient_public_key", "recovery_pub", "sealed", "created_at",
    }:
        raise RootFactorPolicyError("recovery slot has unknown or missing fields")
    created_at = value.get("created_at")
    if not isinstance(created_at, str) or not _ISO8601_Z_RE.match(created_at):
        raise RootFactorPolicyError("recovery slot created_at must be ISO-8601 Z")
    return {
        "recipient_public_key": _public_key(
            value.get("recipient_public_key"), "recovery recipient",
        ),
        "recovery_pub": _public_key(value.get("recovery_pub"), "recovery_pub"),
        "sealed": _b64(_unb64(value.get("sealed"), length=81, what="recovery wrap")),
        "created_at": created_at,
    }


def build_envelope(
    root: KeyPair,
    *,
    generation: int,
    factors: Iterable[Mapping],
    access: Iterable[str],
    policy: object,
    recovery: Mapping | None = None,
) -> dict:
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise RootFactorPolicyError("generation must be a positive integer")
    state = validate_state(factors, access, policy, root_pub=root.public_hex)
    digest = _policy_digest(
        generation, root.public_hex, state["factors"], state["access"], state["policy"]
    )
    recipients = {
        factor["factor_id"]: factor_recipient_public_keys(factor)
        for factor in state["factors"]
    }
    wraps = _compile_node(
        state["policy"], bytes.fromhex(root.private_hex), recipients, digest, "r"
    )
    unsigned = {
        "v": POLICY_VERSION,
        "generation": generation,
        "root_pub": root.public_hex,
        "factors": state["factors"],
        "access": state["access"],
        "policy": state["policy"],
        "wraps": wraps,
    }
    if recovery is not None:
        unsigned["recovery"] = _parse_recovery(recovery)
    return {
        **unsigned,
        "signature": root.sign_hex(POLICY_SIGNATURE_DOMAIN + canonical_json(unsigned)),
    }


def add_recovery_slot(
    envelope: object,
    *,
    root_seed: bytes,
    recovery_recipient_pub: str,
    recovery_pub: str,
    created_at: str = "1970-01-01T00:00:00Z",
) -> dict:
    """Enroll a recovery slot into a v3 armor, re-signed by the root.

    Requires the ROOT SEED (reach root) and the code's PUBLIC halves only — the
    code itself stays cold. Refuses to replace an existing slot: replacement
    requires the old code and is not built (operator ruling).
    """
    parsed = parse_envelope(envelope)
    if "recovery" in parsed:
        raise RootFactorPolicyError(
            "this armor already carries a recovery code; replacing one needs the "
            "old code and is not yet supported"
        )
    root = KeyPair.from_private_hex(bytes(root_seed).hex())
    if root.public_hex != parsed["root_pub"]:
        raise RootFactorPolicyError("the supplied root seed does not match root_pub")
    recipient = _public_key(recovery_recipient_pub, "recovery recipient")
    sealed = seal(bytes(root_seed), recipient, _RECOVERY_ARMOR_PURPOSE)
    recovery = {
        "recipient_public_key": recipient,
        "recovery_pub": _public_key(recovery_pub, "recovery_pub"),
        "sealed": _b64(sealed),
        "created_at": created_at,
    }
    unsigned = {key: parsed[key] for key in (
        "v", "generation", "root_pub", "factors", "access", "policy", "wraps",
    )}
    unsigned["recovery"] = _parse_recovery(recovery)
    return {
        **unsigned,
        "signature": root.sign_hex(POLICY_SIGNATURE_DOMAIN + canonical_json(unsigned)),
    }


def replace_recovery_slot(
    envelope: object,
    *,
    old_code: bytes,
    root_seed: bytes,
    recovery_recipient_pub: str,
    recovery_pub: str,
    created_at: str = "1970-01-01T00:00:00Z",
) -> dict:
    """Rotate the recovery code: the OLD code AND root, per succession closure.

    Requires proving possession of the current code (it must open the existing
    slot) and reaching root (the seed re-signs). Replaces the slot in place.
    The lost-code path (no old code) is the timelocked-regeneration substrate,
    deferred.
    """
    parsed = parse_envelope(envelope)
    if "recovery" not in parsed:
        raise RootFactorPolicyError("this armor carries no recovery code to replace")
    opened = open_root_with_recovery(parsed, old_code)  # proves possession of the old code
    if opened.public_hex != parsed["root_pub"]:
        raise RootFactorPolicyError("the old recovery code did not open this armor")
    root = KeyPair.from_private_hex(bytes(root_seed).hex())
    if root.public_hex != parsed["root_pub"]:
        raise RootFactorPolicyError("the supplied root seed does not match root_pub")
    recipient = _public_key(recovery_recipient_pub, "recovery recipient")
    sealed = seal(bytes(root_seed), recipient, _RECOVERY_ARMOR_PURPOSE)
    unsigned = {key: parsed[key] for key in (
        "v", "generation", "root_pub", "factors", "access", "policy", "wraps",
    )}
    unsigned["recovery"] = _parse_recovery({
        "recipient_public_key": recipient,
        "recovery_pub": _public_key(recovery_pub, "recovery_pub"),
        "sealed": _b64(sealed),
        "created_at": created_at,
    })
    return {
        **unsigned,
        "signature": root.sign_hex(POLICY_SIGNATURE_DOMAIN + canonical_json(unsigned)),
    }


def open_root_with_recovery(envelope: object, recovery_code: bytes) -> KeyPair:
    """Open the root seed with the printed code alone (the emergency floor)."""
    from .recovery import derive_recovery_factors

    parsed = parse_envelope(envelope)
    recovery = parsed.get("recovery")
    if recovery is None:
        raise RootFactorPolicyError(
            "this armor carries no recovery code; a code cannot open it"
        )
    kek_seed = derive_recovery_factors(recovery_code)["kek_recovery_seed"]
    private_hex, public_hex = derive_encapsulation_keypair(kek_seed, _RECOVERY_ARMOR_PURPOSE)
    if public_hex != recovery["recipient_public_key"]:
        raise RootFactorPolicyError("that recovery code does not match this armor")
    try:
        seed = open_sealed(
            _unb64(recovery["sealed"], length=81, what="recovery wrap"),
            private_hex, _RECOVERY_ARMOR_PURPOSE,
        )
    except RootFactorPolicyError:
        raise
    except Exception as exc:
        raise RootFactorPolicyError("the recovery slot does not open with that code") from exc
    seed = bytearray(seed)
    try:
        root = KeyPair.from_private_hex(bytes(seed).hex())
        if root.public_hex != parsed["root_pub"]:
            raise RootFactorPolicyError("opened root seed does not match root_pub")
        return root
    finally:
        seed[:] = b"\x00" * len(seed)


def _validate_wrap_tree(
    wrap: object, policy: dict, factors: Mapping[str, dict],
) -> dict:
    if not isinstance(wrap, dict) or wrap.get("op") != policy["op"]:
        raise RootFactorPolicyError("policy wrap tree does not match its expression")
    if policy["op"] == "factor":
        if set(wrap) != {"op", "factor_id", "sealed"} \
                or wrap.get("factor_id") != policy["factor_id"]:
            raise RootFactorPolicyError("factor wrap does not match its policy leaf")
        sealed = wrap.get("sealed")
        if not isinstance(sealed, list):
            raise RootFactorPolicyError("factor policy wraps must be an array")
        parsed = []
        for row in sealed:
            if not isinstance(row, dict) or set(row) != {
                "recipient_public_key", "sealed",
            }:
                raise RootFactorPolicyError("factor policy recipient wrap is malformed")
            parsed.append({
                "recipient_public_key": _public_key(
                    row.get("recipient_public_key"), "factor policy recipient",
                ),
                "sealed": _b64(_unb64(
                    row.get("sealed"), length=81, what="factor policy wrap",
                )),
            })
        parsed.sort(key=lambda row: row["recipient_public_key"])
        expected = factor_recipient_public_keys(factors[policy["factor_id"]])
        if [row["recipient_public_key"] for row in parsed] != expected:
            raise RootFactorPolicyError(
                "factor policy wraps do not match the factor's recipient set"
            )
        return {"op": "factor", "factor_id": policy["factor_id"], "sealed": parsed}
    if set(wrap) != {"op", "children"} or not isinstance(wrap.get("children"), list) \
            or len(wrap["children"]) != len(policy["children"]):
        raise RootFactorPolicyError("policy wrap branches do not match the expression")
    return {
        "op": policy["op"],
        "children": [
            _validate_wrap_tree(child_wrap, child_policy, factors)
            for child_wrap, child_policy in zip(wrap["children"], policy["children"])
        ],
    }


def parse_envelope(value: object) -> dict:
    base = {
        "v", "generation", "root_pub", "factors", "access", "policy", "wraps",
        "signature",
    }
    if not isinstance(value, dict) or set(value) - {"recovery"} != base:
        raise RootFactorPolicyError("root-factor envelope has unknown or missing fields")
    if value.get("v") != POLICY_VERSION:
        raise RootFactorPolicyError("unsupported root-factor policy version")
    generation = value.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise RootFactorPolicyError("generation must be a positive integer")
    root_pub = _public_key(value.get("root_pub"), "root_pub")
    state = validate_state(
        value.get("factors") if isinstance(value.get("factors"), list) else [],
        value.get("access") if isinstance(value.get("access"), list) else [],
        value.get("policy"),
        root_pub=root_pub,
    )
    factor_map = {factor["factor_id"]: factor for factor in state["factors"]}
    wraps = _validate_wrap_tree(value.get("wraps"), state["policy"], factor_map)
    signature = value.get("signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{128}", signature):
        raise RootFactorPolicyError("root-factor signature must be 128 lowercase hex chars")
    parsed = {
        "v": POLICY_VERSION,
        "generation": generation,
        "root_pub": root_pub,
        "factors": state["factors"],
        "access": state["access"],
        "policy": state["policy"],
        "wraps": wraps,
        "signature": signature,
    }
    if "recovery" in value:
        parsed["recovery"] = _parse_recovery(value.get("recovery"))
    try:
        verify_signature(
            root_pub,
            signature,
            POLICY_SIGNATURE_DOMAIN + canonical_json(_unsigned_envelope(parsed)),
        )
    except Exception as exc:
        raise RootFactorPolicyError(
            "root-factor policy signature does not verify"
        ) from exc
    return parsed


def _open_node(
    wrap: dict,
    seeds: Mapping[str, bytes | bytearray],
    factors: Mapping[str, dict],
    digest: str,
    path: str,
) -> bytearray:
    if wrap["op"] == "factor":
        factor_id = wrap["factor_id"]
        if factor_id not in seeds:
            raise RootFactorPolicyError(f"factor {factor_id!r} was not supplied")
        seed = seeds[factor_id]
        private_key, public_key = derive_encapsulation_keypair(
            seed, FACTOR_RECIPIENT_PURPOSE
        )
        if public_key not in factor_recipient_public_keys(factors[factor_id]):
            raise RootFactorPolicyError(f"factor {factor_id!r} does not match its recipient")
        sealed = next(
            row["sealed"] for row in wrap["sealed"]
            if row["recipient_public_key"] == public_key
        )
        try:
            return bytearray(open_sealed(
                _unb64(sealed, length=81, what="factor policy wrap"),
                private_key,
                _wrap_purpose(digest, path),
            ))
        except Exception as exc:
            raise RootFactorPolicyError(f"factor {factor_id!r} did not open its share") from exc
    if wrap["op"] == "or":
        errors = []
        for index, child in enumerate(wrap["children"]):
            try:
                return _open_node(child, seeds, factors, digest, f"{path}.{index}")
            except RootFactorPolicyError as exc:
                errors.append(exc)
        raise RootFactorPolicyError("no supplied factor satisfied an OR branch") from errors[-1]
    shares = []
    try:
        for index, child in enumerate(wrap["children"]):
            shares.append(_open_node(child, seeds, factors, digest, f"{path}.{index}"))
        return _xor(shares)
    finally:
        for share in shares:
            share[:] = b"\x00" * len(share)


def open_envelope(
    envelope: object,
    factor_seeds: Mapping[str, bytes | bytearray],
) -> KeyPair:
    parsed = parse_envelope(envelope)
    digest = _policy_digest(
        parsed["generation"], parsed["root_pub"], parsed["factors"],
        parsed["access"], parsed["policy"],
    )
    factors = {factor["factor_id"]: factor for factor in parsed["factors"]}
    seed = _open_node(parsed["wraps"], factor_seeds, factors, digest, "r")
    try:
        root = KeyPair.from_private_hex(bytes(seed).hex())
        if root.public_hex != parsed["root_pub"]:
            raise RootFactorPolicyError("opened root seed does not match root_pub")
        return root
    finally:
        seed[:] = b"\x00" * len(seed)


def emit_armored_envelope(envelope: object) -> str:
    parsed = parse_envelope(envelope)
    body = canonical_json({"v": ARMOR_VERSION, "factor_policy": parsed})
    encoded = base64.b64encode(body).decode("ascii")
    return "\n".join([ARMOR_BEGIN, *textwrap.wrap(encoded, 64), ARMOR_END])


def parse_armored_envelope(armor: object) -> dict:
    if not isinstance(armor, str):
        raise RootFactorPolicyError("root-factor armor must be text")
    lines = [line.strip() for line in armor.strip().splitlines() if line.strip()]
    if len(lines) < 3 or lines[0] != ARMOR_BEGIN or lines[-1] != ARMOR_END:
        raise RootFactorPolicyError("root-factor armor is missing its BEGIN/END lines")

    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RootFactorPolicyError(f"root-factor armor repeats field {key!r}")
            result[key] = value
        return result

    encoded = "".join(lines[1:-1])
    try:
        raw = base64.b64decode(encoded, validate=True)
        if base64.b64encode(raw).decode("ascii") != encoded:
            raise RootFactorPolicyError("root-factor armor base64 is not canonical")
        body = json.loads(raw, object_pairs_hook=no_duplicates)
    except RootFactorPolicyError:
        raise
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise RootFactorPolicyError("root-factor armor body does not decode") from exc
    if not isinstance(body, dict) or set(body) != {"v", "factor_policy"} \
            or body.get("v") != ARMOR_VERSION:
        raise RootFactorPolicyError(
            "root-factor armor must carry exactly v=3 and factor_policy"
        )
    parsed = parse_envelope(body.get("factor_policy"))
    canonical = canonical_json({"v": ARMOR_VERSION, "factor_policy": parsed})
    if raw != canonical:
        raise RootFactorPolicyError("root-factor armor JSON is not canonical")
    return parsed


def canonicalize_armored_envelope(armor: object) -> str:
    return emit_armored_envelope(parse_armored_envelope(armor))
