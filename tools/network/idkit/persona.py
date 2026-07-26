"""Per-organization persona derivation.

A persona is the Ed25519 operating key every actor — the owner included —
signs day-to-day ledger actions with, so authority inside an organization
is exercised by a persona, never the personal root key directly.

Derivation is frozen (authority-model note ``b394e84c-b4a``)::

    HKDF-SHA256(personal_root_seed,
                salt=PERSONA_SALT,
                info=genesis_id) -> 32-byte Ed25519 seed

Anchored on the stable ``genesis_id`` (the content hash of the genesis
event), not the rotatable ``root_pub``: deterministic for a given person
and organization, unlinkable across organizations.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import MalformedError
from .keys import KeyPair, _decode_hex

#: Versioned HKDF domain separator — frozen; changing it re-keys every persona.
PERSONA_SALT = b"autonomy.identity.persona.v1"
PERSONA_SEED_LEN = 32

_GENESIS_ID_HEX_LEN = 64  # SHA-256 hexdigest


def derive_persona(personal_root_seed: bytes, genesis_id: str) -> KeyPair:
    """Derive the persona :class:`KeyPair` for one organization.

    *personal_root_seed* is the raw 32-byte Ed25519 seed (what
    ``armor.decrypt_root_key`` yields: ``bytes.fromhex(kp.private_hex)``);
    *genesis_id* is the organization's 64-lowercase-hex genesis event id.
    Raises :class:`MalformedError` on anything else.
    """
    if not isinstance(personal_root_seed, bytes) or len(personal_root_seed) != PERSONA_SEED_LEN:
        raise MalformedError(
            f"personal_root_seed must be exactly {PERSONA_SEED_LEN} raw bytes"
        )
    # Validation only — the derivation is anchored on the string form.
    _decode_hex(genesis_id, _GENESIS_ID_HEX_LEN, "genesis_id")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=PERSONA_SEED_LEN,
        salt=PERSONA_SALT,
        info=genesis_id.encode("ascii"),
    ).derive(personal_root_seed)
    return KeyPair.from_private_hex(derived.hex())
