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

from tools.network.idkit import sealing
from tools.network.idkit.canonical import canonical_json
from tools.network.storagekit import object_header, suites
from tools.network.storagekit.errors import StorageError

from .errors import VaultError
from .policy_class import seal_cek
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

#: The dedicated delegate audited-recipient X25519 encapsulation keypair is
#: derived from the personal root seed under this label. ``derive_encapsulation_keypair``
#: keys the keypair on the label alone, so this delegate opener is independent of
#: every signing key and of the persona/member KEM by construction — a compromise of
#: it never reaches other personal-scope material. Its PUBLIC half is published so an
#: audited write seals COLD; its PRIVATE half warms at unlock and opens the read.
DELEGATE_AUDITED_DERIVE_PURPOSE = "autonomy/vault/delegate-audited-recipient/v1"

#: Bound into the HPKE context when sealing an audited CEK to the delegate; the
#: per-object suffix binds each sealed key to its own object and revision, so a
#: sealed key never opens against another object.
_DELEGATE_AUDITED_SEAL_PURPOSE = "autonomy/vault/delegate-audited-cek/v1"

_AUDITED_FIELDS = frozenset(
    {
        "v",
        "tier",
        "object_id",
        "revision_id",
        "body_suite_id",
        "nonce",
        "ciphertext",
        "delegate_sealed_cek",
    }
)


def derive_delegate_audited_recipient(personal_root_seed: bytes) -> tuple[str, str]:
    """The delegate audited-recipient X25519 keypair as ``(private_hex, public_hex)``.

    An ENCAPSULATION keypair (never a signing key), deterministic from the personal
    root seed under :data:`DELEGATE_AUDITED_DERIVE_PURPOSE` and independent of every
    other key by its label. Publish the public half for the cold write; re-derive the
    private half at unlock for the unattended read.
    """
    return sealing.derive_encapsulation_keypair(
        personal_root_seed, DELEGATE_AUDITED_DERIVE_PURPOSE
    )


def _delegate_seal_purpose(object_id: str, revision_id: str) -> str:
    return f"{_DELEGATE_AUDITED_SEAL_PURPOSE}|{_GENESIS_ID}|{object_id}|{revision_id}"


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
    body_suite_id: str = suites.BODY_SUITE_LARGE,
) -> str:
    """Seal using public material only; no factor or warm key is consulted.

    Every object this function seals is `secured` (the envelope tier is fixed
    below), so the body defaults to `BODY_SUITE_LARGE` (ChaCha20-Poly1305) — the
    browser-openable suite a secured read needs; an `audited` body, sealed on its
    own path, keeps the nonce-misuse-resistant `BODY_SUITE_DEFAULT`.
    """
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
    content_key: bytes,
):
    """Open one frozen personal revision given its already-unwrapped content key.

    The content key is opened by the operator's browser (B-1: it runs the
    policy-class open locally and hands over only this one revision's CEK) — so
    this function never sees opener seeds or the class key, only a key that
    decrypts nothing but this immutable revision. The body AEAD binds the
    object/revision identifiers, so a wrong CEK or a tampered body fails closed.
    """
    envelope = _parse(locator, set_id=set_id, key=key)
    if envelope["revision_id"] != revision_id_for(setting_id):
        raise VaultError("personal vault locator belongs to a different revision")
    ciphertext = bytes.fromhex(envelope["ciphertext"])
    content_key = bytearray(content_key)
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
        try:
            plaintext = object_header.open_body(header, content_key, ciphertext)
        except StorageError as exc:
            # A wrong content key or a tampered body fails the body AEAD; report
            # it at the vault boundary as a fail-closed VaultError, never as a
            # bare storage fault or an empty value.
            raise VaultError(
                "personal vault body does not open with this content key"
            ) from exc
    finally:
        content_key[:] = b"\x00" * len(content_key)
    try:
        return json.loads(plaintext)
    except (TypeError, ValueError) as exc:
        raise VaultError("personal vault payload is not valid JSON") from exc


def seal_audited_revision(
    *,
    set_id: str,
    key: str,
    setting_id: str,
    payload,
    delegate_public_hex: str,
    body_suite_id: str = suites.BODY_SUITE_DEFAULT,
) -> str:
    """Seal one personal AUDITED revision COLD, to the delegate's public key.

    Needs no factor and no warm key holder: the fresh per-revision CEK is sealed to
    *delegate_public_hex* (the published dedicated audited recipient) with the idkit
    hybrid seal, so the write succeeds against a cold vault. The body keeps the
    nonce-misuse-resistant default suite — audited is unattended and high-volume —
    and the delegate's private half, warm in ramfs, opens it on read.
    """
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
        sealed_cek = sealing.seal(
            bytes(content_key),
            delegate_public_hex,
            _delegate_seal_purpose(object_id, revision_id),
        )
    finally:
        content_key[:] = b"\x00" * len(content_key)
    return _encode(
        {
            "v": _ENVELOPE_VERSION,
            "tier": "audited",
            "object_id": object_id,
            "revision_id": revision_id,
            "body_suite_id": body_suite_id,
            "nonce": nonce.hex(),
            "ciphertext": ciphertext.hex(),
            "delegate_sealed_cek": sealed_cek.hex(),
        }
    )


def _parse_audited(locator, *, set_id: str, key: str) -> dict:
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
    if not isinstance(envelope, dict) or set(envelope) != _AUDITED_FIELDS:
        raise VaultError("personal vault locator has an unexpected field set")
    if envelope["v"] != _ENVELOPE_VERSION or envelope["tier"] != "audited":
        raise VaultError("unsupported personal vault envelope")
    if envelope["object_id"] != object_id_for(set_id, key):
        raise VaultError("personal vault locator belongs to a different Setting")
    for name in ("revision_id", "body_suite_id", "nonce", "ciphertext", "delegate_sealed_cek"):
        if not isinstance(envelope[name], str) or not envelope[name]:
            raise VaultError(f"personal vault locator {name} is invalid")
    try:
        nonce = bytes.fromhex(envelope["nonce"])
        ciphertext = bytes.fromhex(envelope["ciphertext"])
        sealed = bytes.fromhex(envelope["delegate_sealed_cek"])
    except ValueError as exc:
        raise VaultError("personal vault locator has invalid hexadecimal") from exc
    if len(nonce) != object_header.NONCE_LEN or not ciphertext or not sealed:
        raise VaultError("personal vault locator has invalid encrypted body")
    if (
        nonce.hex() != envelope["nonce"]
        or ciphertext.hex() != envelope["ciphertext"]
        or sealed.hex() != envelope["delegate_sealed_cek"]
    ):
        raise VaultError("personal vault locator hexadecimal is not canonical")
    suites.require_suite(envelope["body_suite_id"], suites.BODY_SUITES)
    return envelope


def open_audited_revision(
    locator,
    *,
    set_id: str,
    key: str,
    setting_id: str,
    delegate_private_hex: str,
):
    """Open one personal AUDITED revision with the warm delegate private key."""
    envelope = _parse_audited(locator, set_id=set_id, key=key)
    if envelope["revision_id"] != revision_id_for(setting_id):
        raise VaultError("personal vault locator belongs to a different revision")
    object_id = envelope["object_id"]
    ciphertext = bytes.fromhex(envelope["ciphertext"])
    try:
        content_key = bytearray(
            sealing.open(
                bytes.fromhex(envelope["delegate_sealed_cek"]),
                delegate_private_hex,
                _delegate_seal_purpose(object_id, envelope["revision_id"]),
            )
        )
    except sealing.SealingError as exc:
        raise VaultError("audited CEK does not open with the delegate key") from exc
    try:
        header = _BodyHeader(
            version=object_header.OBJECT_HEADER_VERSION,
            body_suite_id=envelope["body_suite_id"],
            genesis_id=_GENESIS_ID,
            domain_id=_DOMAIN_ID,
            object_id=object_id,
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
