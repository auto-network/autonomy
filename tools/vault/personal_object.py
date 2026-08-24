"""A personal secured Setting sealed directly to its policy class.

Personal storage has one owner.  It therefore needs the owner-at-rest policy
layer (a random per-revision CEK sealed to the policy class) and does not need
an organization's storage generation, authority frontier, delegate signature,
or ledger fold.  The complete encrypted envelope is an opaque scalar in the
Settings row: merge-patch can only replace it whole, and fleet synchronization
already carries it with the row.

This module introduces no cipher.  Payload encryption reuses storagekit's
vetted body AEAD and CEK wrapping reuses the policy-class HPKE construction.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass

from tools.network.idkit.canonical import canonical_json
from tools.network.storagekit import object_header, suites

from .errors import VaultError
from .policy_class import open_cek, seal_cek
from .storage_object import SealedContentKey


LOCATOR_PREFIX = "autonomy.vault.personal.v1."
_ENVELOPE_VERSION = 1
_GENESIS_ID = hashlib.sha256(b"autonomy/personal-owner-at-rest/v1").hexdigest()
_DOMAIN_ID = hashlib.sha256(b"autonomy/personal-vault-content/v1").hexdigest()
# ``seal_body`` binds one value in its storage-state AAD slot. Personal data
# has no storage generation, so this is a fixed domain-separation label, not a
# state id, cached key, delegate frontier, or ledger-derived value.
_STORAGE_STATE_ID = hashlib.sha256(
    b"autonomy/personal-vault/no-storage-generation/v1"
).hexdigest()
_OBJECT_ID_DOMAIN = "autonomy/personal-vault/object-id/v1"
_REVISION_ID_DOMAIN = "autonomy/personal-vault/revision-id/v1"
_FIELDS = frozenset(
    {
        "v",
        "tier",
        "object_id",
        "revision_id",
        "policy_class_id",
        "required_policy",
        "body_suite_id",
        "nonce",
        "ciphertext",
        "sealed_cek",
    }
)


def object_id_for(set_id: str, key: str) -> str:
    return hashlib.sha256(
        canonical_json(
            {"domain": _OBJECT_ID_DOMAIN, "set_id": set_id, "key": key}
        )
    ).hexdigest()


def revision_id_for(setting_id: str) -> str:
    return hashlib.sha256(
        canonical_json(
            {"domain": _REVISION_ID_DOMAIN, "setting_id": setting_id}
        )
    ).hexdigest()


def is_personal_locator(value) -> bool:
    return isinstance(value, str) and value.startswith(LOCATOR_PREFIX)


def _encode(envelope: dict) -> str:
    wire = base64.urlsafe_b64encode(canonical_json(envelope)).decode("ascii")
    return LOCATOR_PREFIX + wire.rstrip("=")


def _parse(locator, *, set_id: str, key: str) -> dict:
    if not is_personal_locator(locator):
        raise VaultError("this is not a personal vault locator")
    encoded = locator[len(LOCATOR_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != encoded:
            raise ValueError("non-canonical base64url")
        envelope = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - malformed wire is one refusal
        raise VaultError(f"personal vault locator does not decode: {exc}") from exc
    if not isinstance(envelope, dict) or set(envelope) != _FIELDS:
        raise VaultError("personal vault locator has an unexpected field set")
    if envelope["v"] != _ENVELOPE_VERSION or envelope["tier"] != "secured":
        raise VaultError("unsupported personal vault envelope")
    if envelope["object_id"] != object_id_for(set_id, key):
        raise VaultError("personal vault locator belongs to a different Setting")
    for name in (
        "revision_id",
        "policy_class_id",
        "required_policy",
        "body_suite_id",
        "nonce",
        "ciphertext",
    ):
        if not isinstance(envelope[name], str) or not envelope[name]:
            raise VaultError(f"personal vault locator {name} is invalid")
    if not isinstance(envelope["sealed_cek"], dict):
        raise VaultError("personal vault locator sealed_cek is invalid")
    try:
        nonce = bytes.fromhex(envelope["nonce"])
        ciphertext = bytes.fromhex(envelope["ciphertext"])
    except ValueError as exc:
        raise VaultError("personal vault locator has invalid hexadecimal") from exc
    if len(nonce) != object_header.NONCE_LEN or not ciphertext:
        raise VaultError("personal vault locator has invalid encrypted body")
    if nonce.hex() != envelope["nonce"] or ciphertext.hex() != envelope["ciphertext"]:
        raise VaultError("personal vault locator hexadecimal is not canonical")
    suites.require_suite(envelope["body_suite_id"], suites.BODY_SUITES)
    return envelope


def inspect_revision(locator, *, set_id: str, key: str) -> SealedContentKey:
    """Return only the human-gated CEK view; ciphertext remains in the row."""
    envelope = _parse(locator, set_id=set_id, key=key)
    return SealedContentKey(
        policy_class_id=envelope["policy_class_id"],
        required_policy=envelope["required_policy"],
        sealed_cek=envelope["sealed_cek"],
    )


def seal_revision(
    *,
    set_id: str,
    key: str,
    setting_id: str,
    payload,
    policy_class,
    body_suite_id: str = suites.BODY_SUITE_DEFAULT,
) -> str:
    """Seal using public material only; no factor or warm key is consulted."""
    object_id = object_id_for(set_id, key)
    revision_id = revision_id_for(setting_id)
    nonce = os.urandom(object_header.NONCE_LEN)
    content_key = bytearray(os.urandom(object_header.CEK_LEN))
    try:
        ciphertext = object_header.seal_body(
            bytes(content_key),
            canonical_json(payload),
            body_suite_id=body_suite_id,
            body_nonce=nonce,
            genesis_id=_GENESIS_ID,
            domain_id=_DOMAIN_ID,
            object_id=object_id,
            revision_id=revision_id,
            storage_state_id=_STORAGE_STATE_ID,
        )
        sealed = seal_cek(
            policy_class,
            content_key,
            genesis_id=_GENESIS_ID,
            setting_name=object_id,
            required_policy=policy_class.policy,
        )
    finally:
        content_key[:] = b"\x00" * len(content_key)
    return _encode(
        {
            "v": _ENVELOPE_VERSION,
            "tier": "secured",
            "object_id": object_id,
            "revision_id": revision_id,
            "policy_class_id": policy_class.class_id,
            "required_policy": policy_class.policy,
            "body_suite_id": body_suite_id,
            "nonce": nonce.hex(),
            "ciphertext": ciphertext.hex(),
            "sealed_cek": sealed,
        }
    )


@dataclass(frozen=True)
class _BodyHeader:
    version: int
    body_suite_id: str
    genesis_id: str
    domain_id: str
    object_id: str
    revision_id: str
    storage_state_id: str
    ciphertext_hash: str
    body_nonce: str


def open_revision(
    locator,
    *,
    set_id: str,
    key: str,
    setting_id: str,
    policy_class,
    opener_seeds: dict[str, bytes],
):
    """Open one frozen personal revision and return its JSON payload."""
    envelope = _parse(locator, set_id=set_id, key=key)
    if envelope["revision_id"] != revision_id_for(setting_id):
        raise VaultError("personal vault locator belongs to a different revision")
    if envelope["policy_class_id"] != policy_class.class_id:
        raise VaultError("the policy class offered is not the one this secret names")
    ciphertext = bytes.fromhex(envelope["ciphertext"])
    content_key = bytearray(
        open_cek(
            policy_class,
            opener_seeds,
            envelope["sealed_cek"],
            genesis_id=_GENESIS_ID,
            setting_name=envelope["object_id"],
            required_policy=envelope["required_policy"],
        )
    )
    try:
        header = _BodyHeader(
            version=object_header.OBJECT_HEADER_VERSION,
            body_suite_id=envelope["body_suite_id"],
            genesis_id=_GENESIS_ID,
            domain_id=_DOMAIN_ID,
            object_id=envelope["object_id"],
            revision_id=envelope["revision_id"],
            storage_state_id=_STORAGE_STATE_ID,
            ciphertext_hash=hashlib.sha256(ciphertext).hexdigest(),
            body_nonce=base64.b64encode(bytes.fromhex(envelope["nonce"])).decode(
                "ascii"
            ),
        )
        plaintext = object_header.open_body(header, content_key, ciphertext)
    finally:
        content_key[:] = b"\x00" * len(content_key)
    try:
        return json.loads(plaintext)
    except (TypeError, ValueError) as exc:
        raise VaultError("personal vault payload is not valid JSON") from exc
