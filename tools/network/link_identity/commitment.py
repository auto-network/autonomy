"""Canonical auto.network identity-pin commitment format (v1)."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from enum import Enum


COMMITMENT_DOMAIN = b"autonomy.link.identity-pin.v1\0"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_FRAGMENT = re.compile(r"^ac=([A-Za-z0-9_-]{22})$")


class LinkBindingKind(str, Enum):
    PERSONAL = "personal"
    PERSONA = "persona"
    ORGANIZATION = "organization"


@dataclass(frozen=True)
class LinkBinding:
    kind: LinkBindingKind
    genesis_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LinkBindingKind):
            raise ValueError("link binding kind is invalid")
        if not isinstance(self.genesis_id, str) or not _HEX.fullmatch(self.genesis_id):
            raise ValueError("link binding genesis_id must be 32-byte lowercase hex")


def _digest(binding: LinkBinding, salt: bytes) -> bytes:
    if not isinstance(salt, bytes) or len(salt) != 16:
        raise ValueError("link identity-pin salt must be exactly 16 bytes")
    material = (
        COMMITMENT_DOMAIN
        + binding.kind.value.encode("ascii")
        + b"\0"
        + bytes.fromhex(binding.genesis_id)
        + salt
    )
    return hashlib.sha256(material).digest()[:16]


def commitment_fragment(binding: LinkBinding, salt: bytes) -> str:
    """Return the compact URL fragment ``ac=<base64url-128-bit-pin>``."""
    encoded = base64.urlsafe_b64encode(_digest(binding, salt)).decode("ascii").rstrip("=")
    return "ac=" + encoded


def verify_fragment(fragment: str, binding: LinkBinding, salt: bytes) -> bool:
    """Constant-time verification of a previously parsed ``ac=`` fragment."""
    import hmac

    match = _FRAGMENT.fullmatch(fragment or "")
    if match is None:
        return False
    expected = commitment_fragment(binding, salt).removeprefix("ac=")
    return hmac.compare_digest(match.group(1), expected)
