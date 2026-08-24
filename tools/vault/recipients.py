"""Stable public recipients used by policy-class key wraps.

Legacy custom classes name individual human factors directly.  The durable
architecture does not: a personal class names the person's root anchor, while
an organization class names organization-scoped personas.  Both are public
X25519 recipients; passwords and passkeys are upstream ways of reaching the
private material, not recipient kinds themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tools.network.idkit.sealing import derive_encapsulation_keypair

from .errors import PolicyClassError


PERSONAL_ROOT_RECIPIENT = "personal-root-anchor"
ORGANIZATION_PERSONA_RECIPIENT = "organization-persona"
_KIND_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
POLICY_RECIPIENT_PURPOSE = "autonomy/vault-policy-recipient/v1"


@dataclass(frozen=True)
class PublishedRecipient:
    recipient_id: str
    recipient_kind: str
    public_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.recipient_id, str) or not self.recipient_id:
            raise PolicyClassError("recipient_id must be a non-empty string")
        if not isinstance(self.recipient_kind, str) or not _KIND_RE.fullmatch(
            self.recipient_kind
        ):
            raise PolicyClassError("recipient_kind must be a lowercase stable name")
        if (
            not isinstance(self.public_key, str)
            or len(self.public_key) != 64
            or any(ch not in "0123456789abcdef" for ch in self.public_key)
        ):
            raise PolicyClassError(
                "recipient public_key must be 64 lowercase hex characters"
            )

    # The current generation wire uses factor_id/factor_type field names for
    # backward compatibility. New governance code consumes this neutral
    # recipient interface; a later wire revision can rename those fields
    # without changing the cryptographic construction.
    @property
    def factor_id(self) -> str:
        return self.recipient_id

    @property
    def factor_type(self) -> str:
        return self.recipient_kind


def _purpose(recipient_kind: str) -> str:
    if not isinstance(recipient_kind, str) or not _KIND_RE.fullmatch(recipient_kind):
        raise PolicyClassError("recipient_kind must be a lowercase stable name")
    return f"{POLICY_RECIPIENT_PURPOSE}|{recipient_kind}"


def recipient_keypair_from_seed(seed: bytes, recipient_kind: str) -> tuple[str, str]:
    """Purpose-derive one policy recipient without reusing an identity key."""
    if not isinstance(seed, (bytes, bytearray)) or len(seed) < 32:
        raise PolicyClassError("recipient seed must be at least 32 bytes")
    return derive_encapsulation_keypair(bytes(seed), _purpose(recipient_kind))


def recipient_public_from_seed(seed: bytes, recipient_kind: str) -> str:
    return recipient_keypair_from_seed(seed, recipient_kind)[1]


def recipient_private_from_seed(seed: bytes, recipient_kind: str) -> str:
    return recipient_keypair_from_seed(seed, recipient_kind)[0]
