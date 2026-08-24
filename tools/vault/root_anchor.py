"""A stable vault recipient reached through the personal root.

The anchor is deliberately separate from both kinds of key the personal root
already has:

* the Ed25519 root remains a signing identity;
* a purpose-derived X25519 recipient, derived from the root seed, opens one
  random 32-byte anchor seed;
* that anchor seed derives a policy-recipient X25519 keypair to which a class
  can wrap its class key. Organization personas use the same recipient seam
  under a different purpose-bound kind.

Consequently root-factor churn changes only the armor.  It never changes this
record or any policy class.  A root rotation re-seals this one anchor seed to
the new root-derived recipient; individual Settings remain untouched.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from tools.network.idkit.canonical import canonical_json
from tools.network.idkit.keys import KeyPair, verify_signature
from tools.network.idkit.sealing import (
    derive_encapsulation_keypair,
    open as seal_open,
    seal,
)

from .errors import FactorError, VaultError
from .recipients import (
    PERSONAL_ROOT_RECIPIENT,
    PublishedRecipient,
    recipient_public_from_seed,
)


ROOT_ANCHOR_WRAP_PURPOSE = "autonomy/vault-root-anchor-wrap/v1"
ROOT_ANCHOR_ENROLL_DOMAIN = b"autonomy/vault-root-anchor-enroll/v1\n"
ROOT_ANCHOR_VERSION = 1
_SEED_LEN = 32
_HEX_32 = 64
# RFC 9180 suite byte + X25519 encapsulated key + 32-byte plaintext + tag.
_SEALED_SEED_HEX_LEN = (1 + 32 + _SEED_LEN + 16) * 2


def _require_hex32(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != _HEX_32:
        raise VaultError(f"root anchor {label} must be 64 lowercase hex characters")
    if any(ch not in "0123456789abcdef" for ch in value):
        raise VaultError(f"root anchor {label} must be 64 lowercase hex characters")
    return value


def wrap_purpose(anchor_id: str, root_pub: str) -> str:
    """Bind a sealed seed to one anchor and one personal-root identity."""
    digest = hashlib.sha256(canonical_json({
        "anchor_id": anchor_id,
        "root_pub": root_pub,
        "v": ROOT_ANCHOR_VERSION,
    })).hexdigest()
    return f"{ROOT_ANCHOR_WRAP_PURPOSE}|{digest}"


@dataclass(frozen=True)
class RootAnchorRecord:
    """The public, root-attested envelope stored in the personal database."""

    anchor_id: str
    display_name: str
    root_pub: str
    public_key: str
    sealed_seed: str
    created_at: str
    signature: str
    v: int = ROOT_ANCHOR_VERSION

    def unsigned_dict(self) -> dict:
        return {
            "v": self.v,
            "anchor_id": self.anchor_id,
            "display_name": self.display_name,
            "root_pub": self.root_pub,
            "public_key": self.public_key,
            "sealed_seed": self.sealed_seed,
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict:
        return {**self.unsigned_dict(), "signature": self.signature}

    @classmethod
    def from_dict(cls, value: dict) -> "RootAnchorRecord":
        if not isinstance(value, dict) or set(value) != {
            "v", "anchor_id", "display_name", "root_pub", "public_key",
            "sealed_seed", "created_at", "signature",
        }:
            raise VaultError("root anchor record has unknown or missing fields")
        if value.get("v") != ROOT_ANCHOR_VERSION:
            raise VaultError(f"unsupported root anchor version {value.get('v')!r}")
        for field in ("anchor_id", "display_name", "created_at"):
            member = value.get(field)
            if not isinstance(member, str) or not member or len(member) > 256:
                raise VaultError(f"root anchor {field} must be a short non-empty string")
        _require_hex32(value.get("root_pub"), "root_pub")
        _require_hex32(value.get("public_key"), "public_key")
        sealed_seed = value.get("sealed_seed")
        if (
            not isinstance(sealed_seed, str)
            or len(sealed_seed) != _SEALED_SEED_HEX_LEN
            or any(ch not in "0123456789abcdef" for ch in sealed_seed)
        ):
            raise VaultError("root anchor sealed_seed is not a canonical HPKE record")
        signature = value.get("signature")
        if (
            not isinstance(signature, str)
            or len(signature) != 128
            or any(ch not in "0123456789abcdef" for ch in signature)
        ):
            raise VaultError("root anchor signature must be 128 lowercase hex characters")
        record = cls(**value)
        try:
            verify_signature(
                record.root_pub,
                record.signature,
                ROOT_ANCHOR_ENROLL_DOMAIN + canonical_json(record.unsigned_dict()),
            )
        except Exception as exc:
            raise VaultError("root anchor is not signed by its personal root") from exc
        return record

    def published_recipient(self) -> PublishedRecipient:
        return PublishedRecipient(
            self.anchor_id, PERSONAL_ROOT_RECIPIENT, self.public_key,
        )


def create_root_anchor(
    root: KeyPair,
    *,
    anchor_id: str,
    display_name: str,
    created_at: str,
    anchor_seed: bytes | None = None,
) -> tuple[RootAnchorRecord, bytes]:
    """Create and root-sign an anchor envelope (Python harness/reference)."""
    seed = bytes(anchor_seed if anchor_seed is not None else secrets.token_bytes(_SEED_LEN))
    if len(seed) != _SEED_LEN:
        raise FactorError("root anchor seed must be exactly 32 bytes")
    purpose = wrap_purpose(anchor_id, root.public_hex)
    _root_private, root_recipient = derive_encapsulation_keypair(
        bytes.fromhex(root.private_hex), purpose,
    )
    unsigned = {
        "v": ROOT_ANCHOR_VERSION,
        "anchor_id": anchor_id,
        "display_name": display_name,
        "root_pub": root.public_hex,
        "public_key": recipient_public_from_seed(seed, PERSONAL_ROOT_RECIPIENT),
        "sealed_seed": seal(
            seed, root_recipient, purpose,
        ).hex(),
        "created_at": created_at,
    }
    signature = root.sign_hex(ROOT_ANCHOR_ENROLL_DOMAIN + canonical_json(unsigned))
    return RootAnchorRecord.from_dict({**unsigned, "signature": signature}), seed


def open_root_anchor(record: RootAnchorRecord, root_seed: bytes) -> bytes:
    """Open *record* after a ceremony produced the matching root seed."""
    purpose = wrap_purpose(record.anchor_id, record.root_pub)
    root_private, _root_public = derive_encapsulation_keypair(root_seed, purpose)
    try:
        seed = seal_open(
            bytes.fromhex(record.sealed_seed),
            root_private,
            purpose,
        )
    except Exception as exc:
        raise VaultError("the opened personal root does not open this vault anchor") from exc
    if (
        len(seed) != _SEED_LEN
        or recipient_public_from_seed(seed, PERSONAL_ROOT_RECIPIENT)
        != record.public_key
    ):
        raise VaultError("the root anchor seed does not match its published recipient")
    return seed
