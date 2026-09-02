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

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import MalformedError
from .keys import KeyPair, _decode_hex

#: Versioned HKDF domain separator — frozen; changing it re-keys every persona.
PERSONA_SALT = b"autonomy.identity.persona.v1"
#: DISTINCT salt for the per-machine operating key (auto-b6fee). A machine's
#: authentication key is HKDF(personal_root, machine_id) — the same shape as a
#: persona, one identity axis over (machine instead of organisation). The salt
#: differs from PERSONA_SALT so a machine key and a persona are cryptographically
#: INDEPENDENT by construction, never by an assumption that a machine id and a
#: genesis id can't collide. Changing it re-keys every machine.
MACHINE_KEY_SALT = b"autonomy.identity.machine.v1"
#: DISTINCT salt for the per-(organization, machine) SERVING key (auto-e2ufw).
#: derive_machine_key above is the personal FLEET key: one identity per machine
#: with NO organization axis, so the same machine presents the same public key
#: to every org — correct for mutually-trusted fleet channels, WRONG as the
#: identity a machine shows an untrusted relay, which could then correlate that
#: machine across organizations. The serving key derives over BOTH the org
#: genesis and the machine id (the same unlinkability shape derive_persona uses
#: over the org axis, extended to also bind the machine), so each org gets a
#: serving-machine identity that is unlinkable across orgs. Its own salt keeps
#: it cryptographically independent of both the persona and the fleet key.
#: Changing it re-keys every serving machine.
SERVING_MACHINE_KEY_SALT = b"autonomy.identity.serving-machine.v1"
PERSONA_SEED_LEN = 32

_GENESIS_ID_HEX_LEN = 64  # SHA-256 hexdigest
_MACHINE_ID_HEX_LEN = 64  # 256-bit random machine identifier


def derive_persona(personal_root_seed: bytes, genesis_id: str) -> KeyPair:
    """Derive the persona :class:`KeyPair` for one organization.

    *personal_root_seed* is the raw 32-byte Ed25519 seed (what
    ``root_factor_policy.open_armor_with_password`` yields:
    ``bytes.fromhex(kp.private_hex)``);
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


def derive_machine_key(personal_root_seed: bytes, machine_id: str) -> KeyPair:
    """Derive a machine's per-machine operating :class:`KeyPair` (auto-b6fee).

    A fleet machine authenticates on fleet channels with a key derived from the
    operator's personal root and the machine's own random identifier —
    ``HKDF-SHA256(personal_root_seed, salt=MACHINE_KEY_SALT, info=machine_id)`` —
    exactly the shape :func:`derive_persona` uses one identity axis over (machine
    instead of organisation). The machine holds no stored key: it re-derives this
    the moment the root is unlocked, and the machine id is public (on the roster).

    Because the salt differs from ``PERSONA_SALT``, a machine key and a persona
    are cryptographically independent even for the same root and colliding ids.
    Any fleet member holding the root can derive any machine's key from its public
    id — this authenticates FLEET MEMBERSHIP, not one machine versus another,
    which is correct: fleet machines are mutually trusted and a stolen machine is
    a root-rotation event regardless.

    *machine_id* is the 64-lowercase-hex identifier assigned by the approving
    dashboard after the one-time verification ceremony. Raises
    :class:`MalformedError` on anything else.
    """
    if not isinstance(personal_root_seed, bytes) or len(personal_root_seed) != PERSONA_SEED_LEN:
        raise MalformedError(
            f"personal_root_seed must be exactly {PERSONA_SEED_LEN} raw bytes"
        )
    _decode_hex(machine_id, _MACHINE_ID_HEX_LEN, "machine_id")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=PERSONA_SEED_LEN,
        salt=MACHINE_KEY_SALT,
        info=machine_id.encode("ascii"),
    ).derive(personal_root_seed)
    return KeyPair.from_private_hex(derived.hex())


def derive_serving_machine_key(
    personal_root_seed: bytes, genesis_id: str, machine_id: str
) -> KeyPair:
    """Derive a machine's per-organization SERVING :class:`KeyPair` (auto-e2ufw).

    This is the identity a machine presents to the (untrusted) relay when it
    serves under one organization. Unlike :func:`derive_machine_key` — the
    personal fleet key with no organization axis — this binds BOTH the org's
    ``genesis_id`` and the machine's ``machine_id``, so the same physical
    machine shows a DIFFERENT, unrelated public key to each organization's
    relay and cannot be correlated across organizations. It is the exact
    unlinkability construction :func:`derive_persona` uses over the org axis,
    extended to also carry the machine::

        HKDF-SHA256(personal_root_seed,
                    salt=SERVING_MACHINE_KEY_SALT,
                    info=genesis_id + "\\0" + machine_id) -> Ed25519 seed

    Both ids are 64-lowercase-hex and fixed-length, and the NUL separator
    cannot occur in either, so the info encoding is unambiguous by
    construction (the string-anchoring discipline this module keeps). The
    distinct salt makes this key cryptographically independent of both the
    persona and the fleet machine key even for the same root and colliding
    ids. Nothing is stored: the machine re-derives it when the root unlocks.

    *genesis_id* is the organization's 64-lowercase-hex genesis event id;
    *machine_id* is the machine's 64-lowercase-hex durable identifier. Raises
    :class:`MalformedError` on anything else.
    """
    if not isinstance(personal_root_seed, bytes) or len(personal_root_seed) != PERSONA_SEED_LEN:
        raise MalformedError(
            f"personal_root_seed must be exactly {PERSONA_SEED_LEN} raw bytes"
        )
    _decode_hex(genesis_id, _GENESIS_ID_HEX_LEN, "genesis_id")
    _decode_hex(machine_id, _MACHINE_ID_HEX_LEN, "machine_id")
    info = (genesis_id + "\0" + machine_id).encode("ascii")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=PERSONA_SEED_LEN,
        salt=SERVING_MACHINE_KEY_SALT,
        info=info,
    ).derive(personal_root_seed)
    return KeyPair.from_private_hex(derived.hex())


def mint_machine_id() -> str:
    """A fresh 256-bit durable machine identifier, 64-lowercase-hex."""
    return os.urandom(_MACHINE_ID_HEX_LEN // 2).hex()
