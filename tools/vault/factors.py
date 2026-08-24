"""Vault factors — the human-gated inputs a policy class wraps its key to.

A *factor* is one way a human authorizes a secured read: a password,
or a WebAuthn passkey's PRF output. Every factor reduces to a 32-byte **seed**;
from that seed a single X25519 *encapsulation keypair* is derived under the
purpose label ``autonomy/vault-factor/v1`` (crib §18). The factor publishes the
public half; the policy class seals its key to that public half. Opening the
class needs the seed, and obtaining the seed needs the human — a password to
open the armor, or a passkey ceremony to produce the PRF output.

Two factor types exist:

* ``password`` — the seed is armored (``idkit.armor``: PBKDF2-HMAC-SHA256,
  600,000 iterations, into AES-256-GCM). The armor is the persisted material;
  the password never is. This is the ONLY factor phase one builds.
* ``passkey`` — the seed IS the WebAuthn PRF extension output. The PRF library
  is out of this epic (crib §18), so this module accepts a raw seed directly so
  the ``prf`` / ``both`` constructions can be *attacked* headlessly with a
  throwaway seed standing in for a real PRF output. No passkey ceremony is
  implemented here.

Nothing here caches a seed. A seed lives only for the duration of the single
call that opens a factor.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from tools.network.idkit.armor import decrypt_root_key, encrypt_root_key
from tools.network.idkit.keys import KeyPair
from tools.network.idkit.sealing import derive_encapsulation_keypair

from .errors import FactorError

#: Purpose label the factor's encapsulation keypair is derived under. Bound
#: into the HKDF info by ``derive_encapsulation_keypair`` so a seed used here
#: is non-interchangeable with the same bytes used for any other purpose.
VAULT_FACTOR_PURPOSE = "autonomy/vault-factor/v1"

PASSWORD = "password"
PASSKEY = "passkey"
FACTOR_TYPES = (PASSWORD, PASSKEY)

_SEED_LEN = 32


@dataclass(frozen=True)
class PublishedFactor:
    """The public identity of a factor — everything class creation needs.

    Creating a class needs only these: it seals to ``public_key`` and never
    touches the seed. That is why "creating a class requires no existing
    factor" (crib §18): the factor pubs are published, class minting consumes
    the pubs, and no prior class is opened.
    """

    factor_id: str
    factor_type: str
    public_key: str  # 64-hex X25519 encapsulation pub under VAULT_FACTOR_PURPOSE

    def __post_init__(self) -> None:
        if self.factor_type not in FACTOR_TYPES:
            raise FactorError(f"unknown factor type {self.factor_type!r}")
        if not (isinstance(self.public_key, str) and len(self.public_key) == 64):
            raise FactorError("factor public_key must be 64 hex chars")


def public_from_seed(seed: bytes) -> str:
    if not isinstance(seed, (bytes, bytearray)) or len(seed) < _SEED_LEN:
        raise FactorError(f"factor seed must be at least {_SEED_LEN} bytes")
    _, public_hex = derive_encapsulation_keypair(bytes(seed), VAULT_FACTOR_PURPOSE)
    return public_hex


# Kept as an internal alias for callers written before the public root-anchor
# seam existed.
_public_from_seed = public_from_seed


def factor_private_from_seed(seed: bytes) -> str:
    """The X25519 private key (64-hex) a factor opens its wrap with."""
    if not isinstance(seed, (bytes, bytearray)) or len(seed) < _SEED_LEN:
        raise FactorError(f"factor seed must be at least {_SEED_LEN} bytes")
    private_hex, _ = derive_encapsulation_keypair(bytes(seed), VAULT_FACTOR_PURPOSE)
    return private_hex


# ── password factor (armor-backed) ────────────────────────────────────────


@dataclass(frozen=True)
class PasswordFactor:
    """A newly minted password factor: its public identity plus the armor.

    ``armor`` is the persisted, password-protected material. Store it; store
    ``published`` in the class's factor roster. The password is held by nobody.
    """

    published: PublishedFactor
    armor: str


def create_password_factor(password: str, *, factor_id: str) -> PasswordFactor:
    """Mint a password factor: random seed, armored under *password*.

    The seed is a fresh Ed25519 private seed (32 bytes) armored by
    ``idkit.armor``; the vault-factor encapsulation pub is derived from it and
    published. Creating this factor is an INTENT act (enrolling a factor) and
    is gated on the human typing the password — see crib §9 Q0.
    """
    if not isinstance(password, str) or not password:
        raise FactorError("password must be a non-empty string")
    if not isinstance(factor_id, str) or not factor_id:
        raise FactorError("factor_id must be a non-empty string")
    keypair = KeyPair.generate()
    seed = bytes.fromhex(keypair.private_hex)
    armor = encrypt_root_key(keypair, password)
    return PasswordFactor(
        published=PublishedFactor(factor_id, PASSWORD, _public_from_seed(seed)),
        armor=armor,
    )


def open_password_seed(armor: str, password: str) -> bytes:
    """Open a password factor's armor with *password*; return its 32-byte seed.

    Raises :class:`FactorError` on a wrong password or a tampered armor — the
    idkit taxonomy failure is normalized so callers cannot distinguish the two.
    """
    try:
        keypair = decrypt_root_key(armor, password)
    except Exception as exc:  # noqa: BLE001 — normalize to one failure shape
        raise FactorError("password factor did not open") from exc
    return bytes.fromhex(keypair.private_hex)


# ── passkey factor (PRF output; PRF library out of epic) ──────────────────


def create_passkey_factor(prf_seed: bytes, *, factor_id: str) -> PublishedFactor:
    """Publish a passkey factor from a raw *prf_seed* (a PRF-output stand-in).

    The real seed is a WebAuthn PRF extension output; that library is out of
    this epic (crib §18). This exists so ``prf`` / ``both`` classes can be
    *attacked* headlessly — it is NOT a passkey ceremony and must not be wired
    to one until the PRF library lands its own cryptographic review.
    """
    if not isinstance(factor_id, str) or not factor_id:
        raise FactorError("factor_id must be a non-empty string")
    return PublishedFactor(factor_id, PASSKEY, _public_from_seed(prf_seed))


def random_seed() -> bytes:
    """A throwaway 32-byte seed — a passkey PRF-output stand-in for tests."""
    return secrets.token_bytes(_SEED_LEN)
