"""Armor facade — version dispatch over the personal root's armored envelope.

Every live armor is a version-3 root factor policy
(:mod:`tools.network.idkit.root_factor_policy`); this module is the thin
front door that classifies an armor text and routes to the policy module,
plus the error taxonomy every opener shares. The retired version-2 identity
armor implementation was deleted; a v2 (or v1) armor text is refused with
:class:`ArmorError` — no code path opens or mints one.
"""

from __future__ import annotations

import base64
import binascii
import json

from .errors import IdkitError

ARMOR_BEGIN = "-----BEGIN AUTONOMY NETWORK ROOT KEY-----"
ARMOR_END = "-----END AUTONOMY NETWORK ROOT KEY-----"

_RETIRED = (
    "this armor is in a retired format (version {v!r}); only version-3 "
    "root factor policies can be opened or described"
)

class ArmorError(IdkitError):
    """The armor cannot be parsed or decrypted."""


class ArmorPassphraseError(ArmorError):
    """GCM authentication failed — wrong passphrase or corrupted blob."""


def _armor_body(armor: str) -> dict:
    """Shared decode: BEGIN/END unwrap, canonical base64, duplicate-key-refusing
    JSON parse. Returns the raw dict; VERSION-SPECIFIC field-closure is the
    caller's job, so the one anti-malleability boundary is not duplicated
    across format versions.
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
    """Strict-parse an armor and re-emit it in the one canonical byte form.

    Unlike v1 this does not rebuild the body field by field, because it does
    not need to: the version-3 parser is closed to an EXACT key set at every
    level, including a per-type strict parser for each factor, so anything the
    parse accepted already contains nothing else. Re-emitting the parsed dict
    as canonical JSON is therefore complete by construction --- and stays
    complete when a factor type is added, which a hand-copied field list here
    would not.
    """
    body = _armor_body(armor)
    if body.get("v") == 3:
        from .root_factor_policy import canonicalize_armored_envelope
        try:
            return canonicalize_armored_envelope(armor)
        except ValueError as exc:
            raise ArmorError(str(exc)) from exc
    raise ArmorError(_RETIRED.format(v=body.get('v')))


def armor_factor_types(armor: str) -> list:
    """Which locks this armor carries, in declaration order."""
    body = _armor_body(armor)
    if body.get("v") == 3:
        from .root_factor_policy import parse_armored_envelope, policy_factor_ids
        try:
            envelope = parse_armored_envelope(armor)
        except ValueError as exc:
            raise ArmorError(str(exc)) from exc
        members = set(policy_factor_ids(envelope["policy"]))
        return [
            factor["type"] for factor in envelope["factors"]
            if factor["factor_id"] in members
        ]
    raise ArmorError(_RETIRED.format(v=body.get('v')))


def armor_version(armor: str) -> int:
    """The declared version of *armor*, else :class:`ArmorError`.

    This stays as a named check rather than an inline comparison because the
    parse it performs is strict for every supported version.
    """
    v = _armor_body(armor).get("v")
    if v == 3:
        from .root_factor_policy import parse_armored_envelope
        try:
            parse_armored_envelope(armor)
        except ValueError as exc:
            raise ArmorError(str(exc)) from exc
        return v
    raise ArmorError(_RETIRED.format(v=v))


def armor_root_pub(armor: str) -> str:
    """The bound ``root_pub`` of an armor, WITHOUT decrypting it.

    For the call sites that need only the identity, not the key. Strict-parses,
    so a malformed blob is still refused.
    """
    body = _armor_body(armor)
    if body.get("v") == 3:
        from .root_factor_policy import parse_armored_envelope
        try:
            return parse_armored_envelope(armor)["root_pub"]
        except ValueError as exc:
            raise ArmorError(str(exc)) from exc
    raise ArmorError(_RETIRED.format(v=body.get('v')))
