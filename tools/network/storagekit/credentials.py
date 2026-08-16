"""Persona key-encapsulation credential — the address for capability grants.

A persona publishes a signed credential binding a per-organization X25519
encapsulation public key to the organization identity (contract §5). The
encapsulation keypair is derived independently of the Ed25519 signing key
(via the sealing primitive's purpose-labelled derivation, F2 discipline:
the two key kinds never cross), the credential is Ed25519-signed by the
persona signing key, and grants are addressed to ``kem_key_id`` — the
SHA-256 of the canonical binding.

Currency: a credential is valid only while its persona holds content
access under the fold; at most one credential is current per persona.
Supersession runs over the cited ``authority_heads`` frontiers — strict
causal descent supersedes; equal or incomparable frontiers resolve by
ascending ``kem_key_id`` with the greatest current, matching the
ledger's ascending-identifier merge order. Compromise of the
encapsulation private key alone is remediated by ``member.rekey``: the
key change retires the credential (its persona key is no longer any
member's current key), and the persona publishes a fresh credential
under its new signing key.

Also here: ``domain_member_keys``, the single contract §4 roster
projection every storage predicate consumes. The organization root
signing key is not a domain principal — it publishes no credential and
receives no grants.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields, replace

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from tools.network.idkit import canonical_json, load_public_key
from tools.network.idkit import KeyPair
from tools.network.idkit import verify_signature as idkit_verify_signature
from tools.network.idkit.errors import IdkitError
from tools.network.idkit.sealing import derive_encapsulation_keypair

from . import suites
from .errors import MalformedRecordError, RecordSignatureError, StorageError
from .records import parse_canonical, signing_input
from .state import _require_hex, _require_id_tuple

CREDENTIAL_VERSION = 1
CREDENTIAL_DOMAIN = b"autonomy.storage.persona-kem-credential.v1\n"
SUITE_ID = suites.SEAL_SUITE

#: Purpose prefix for the encapsulation derivation — the genesis id is
#: appended so the keypair is bound to one organization.
KEM_PURPOSE_PREFIX = "autonomy/persona-kem/v1/"

#: HKDF info prefix for the persona KEM *seed* — the counter is appended so
#: PersonaKemCredential rotation (auto-biqme) can advance the seed without
#: touching the fixed :func:`kem_purpose` label. Distinct label space from
#: ``KEM_PURPOSE_PREFIX``: this derives the seed handed to :func:`build`, and
#: ``build`` binds that seed to one organization via ``kem_purpose(genesis_id)``.
KEM_SEED_INFO_PREFIX = "autonomy/persona-storage-kem-seed/v1/"

_ID_HEX_LEN = 64
_KEY_HEX_LEN = 64
_SIG_HEX_LEN = 128


class CredentialError(StorageError):
    """The credential is not acceptable against the authority fold."""


def kem_purpose(genesis_id: str) -> str:
    return KEM_PURPOSE_PREFIX + genesis_id


def domain_member_keys(fold) -> frozenset:
    """The contract §4 domain principal set: current keys of claimed
    members currently holding a content-granting role (version one treats
    every role as content-granting). The root signing key is never here."""
    return frozenset(m.current_key for m in fold.members.values() if m.roles)


@dataclass(frozen=True)
class PersonaKemCredential:
    version: int
    suite_id: int
    genesis_id: str
    persona: str  # Ed25519 signing public hex — the record's signer
    kem_public_key: str  # X25519 encapsulation public hex — never a signer
    kem_key_id: str
    authority_heads: tuple
    created_hlc: tuple  # (ts_ms, count), advisory
    signature: str

    def binding_dict(self) -> dict:
        return {
            "version": self.version,
            "suite_id": self.suite_id,
            "genesis_id": self.genesis_id,
            "persona": self.persona,
            "kem_public_key": self.kem_public_key,
            "authority_heads": list(self.authority_heads),
            "created_hlc": list(self.created_hlc),
        }

    def signed_dict(self) -> dict:
        return {**self.binding_dict(), "kem_key_id": self.kem_key_id}

    def signing_input(self) -> bytes:
        return signing_input(CREDENTIAL_DOMAIN, self.signed_dict())

    def to_dict(self) -> dict:
        return {**self.signed_dict(), "signature": self.signature}

    def to_json(self) -> bytes:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, wire: bytes) -> "PersonaKemCredential":
        data = parse_canonical(_FIELDS, wire)
        return _from_fields(data)


_FIELDS = tuple(f.name for f in fields(PersonaKemCredential))


def _from_fields(data: dict) -> PersonaKemCredential:
    for name in ("authority_heads", "created_hlc"):
        if not isinstance(data[name], (list, tuple)):
            raise MalformedRecordError(f"{name} must be a list")
    return PersonaKemCredential(
        **{
            **data,
            "authority_heads": tuple(data["authority_heads"]),
            "created_hlc": tuple(data["created_hlc"]),
        }
    )


def compute_kem_key_id(binding: dict) -> str:
    return hashlib.sha256(canonical_json(binding)).hexdigest()


def derive_kem_seed(root_seed: bytes, counter: int = 0) -> bytes:
    """Derive a persona's KEM seed from the personal root seed.

    The seed feeds :func:`build`, which binds the resulting X25519
    encapsulation keypair to one organization via
    :func:`kem_purpose` — so the seed itself is org-independent and
    ``counter`` is the only rotation input. The initial publication (this
    program's founding and admission paths) uses counter zero;
    PersonaKemCredential rotation (auto-biqme, contract §1d) advances it.

    Deterministic and secretless-at-rest: the same root and counter yield a
    BYTE-IDENTICAL seed on every machine, and thus a byte-identical keypair,
    with no device-local randomness (contract §14 — "same root + same
    kem_seed ⇒ byte-identical keypair on every machine"). Never persisted; a
    second machine re-derives it from the armored personal root at unlock
    (§1c), so no enrolment ceremony and no key-control record is needed to
    add a device.
    """
    if not isinstance(root_seed, (bytes, bytearray)):
        raise MalformedRecordError("root_seed must be bytes")
    if type(counter) is not int or counter < 0:
        raise MalformedRecordError("counter must be a non-negative integer")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=(KEM_SEED_INFO_PREFIX + str(counter)).encode("ascii"),
    ).derive(bytes(root_seed))


def build(
    signing: KeyPair,
    genesis_id: str,
    kem_seed: bytes,
    authority_heads,
    created_hlc,
    *,
    suite_id: int = SUITE_ID,
) -> tuple:
    """Derive the org-bound encapsulation keypair, bind, and sign.

    Returns ``(credential, kem_private_hex)`` — the private key goes to
    the caller's device store, never into the record.

    Seed hygiene (caller contract): the KEM keypair is a function of
    ``(kem_seed, genesis_id)`` only — two personas sharing a seed in one
    org share a keypair. Give each persona its own seed.
    """
    _require_hex(genesis_id, _ID_HEX_LEN, "genesis_id")
    kem_private, kem_public = derive_encapsulation_keypair(
        kem_seed, kem_purpose(genesis_id)
    )
    unsigned = PersonaKemCredential(
        version=CREDENTIAL_VERSION,
        suite_id=suite_id,
        genesis_id=genesis_id,
        persona=signing.public_hex,
        kem_public_key=kem_public,
        kem_key_id="",
        authority_heads=_require_id_tuple(sorted(set(authority_heads)), "authority_heads"),
        created_hlc=_require_hlc(created_hlc),
        signature="0" * _SIG_HEX_LEN,
    )
    bound = replace(unsigned, kem_key_id=compute_kem_key_id(unsigned.binding_dict()))
    credential = replace(bound, signature=signing.sign_hex(bound.signing_input()))
    return validate(credential), kem_private


def _require_hlc(value) -> tuple:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(v) is not int or v < 0 for v in value)
    ):
        raise MalformedRecordError("created_hlc must be [ts_ms, count], non-negative ints")
    return tuple(value)


def validate(record) -> PersonaKemCredential:
    """Structural validation, every failure a StorageError subclass.

    Accepts the canonical wire bytes, the embedded ``kem_credential``
    payload dict (as the member.claim validator holds it), or a
    :class:`PersonaKemCredential`.
    """
    if isinstance(record, (bytes, bytearray)):
        credential = PersonaKemCredential.from_json(bytes(record))
    elif isinstance(record, dict):
        if set(record) != set(_FIELDS):
            missing = sorted(set(_FIELDS) - set(record))
            unknown = sorted(set(record) - set(_FIELDS))
            raise MalformedRecordError(
                f"credential fields do not match: missing {missing}, unknown {unknown}"
            )
        credential = _from_fields(dict(record))
    elif isinstance(record, PersonaKemCredential):
        credential = record
    else:
        raise MalformedRecordError("credential must be wire bytes, a dict, or a record")

    if credential.version != CREDENTIAL_VERSION:
        raise MalformedRecordError(f"unsupported credential version: {credential.version!r}")
    suites.require_suite(credential.suite_id, suites.SEAL_SUITES)
    _require_hex(credential.genesis_id, _ID_HEX_LEN, "genesis_id")
    _require_hex(credential.persona, _KEY_HEX_LEN, "persona")
    try:
        load_public_key(credential.persona)
    except IdkitError as exc:
        raise MalformedRecordError("persona is not a valid Ed25519 public key") from exc
    _require_hex(credential.kem_public_key, _KEY_HEX_LEN, "kem_public_key")
    _require_hex(credential.kem_key_id, _ID_HEX_LEN, "kem_key_id")
    _require_id_tuple(credential.authority_heads, "authority_heads")
    _require_hlc(credential.created_hlc)
    if compute_kem_key_id(credential.binding_dict()) != credential.kem_key_id:
        raise MalformedRecordError("kem_key_id does not match the credential binding")
    _require_hex(credential.signature, _SIG_HEX_LEN, "signature")
    try:
        idkit_verify_signature(
            credential.persona, credential.signature, credential.signing_input()
        )
    except IdkitError as exc:
        raise RecordSignatureError("credential signature does not verify") from exc
    return credential


def verify_against_fold(record, fold) -> PersonaKemCredential:
    """Accept only a credential for a current domain principal.

    Rejects a rekey-retired key (no longer any member's current key), a
    revoked or role-stripped persona, a non-member key, the organization
    root signing key, and a cross-organization ``genesis_id``.
    """
    credential = validate(record)
    if credential.genesis_id != fold.genesis_id:
        raise CredentialError("credential is bound to a different organization")
    if credential.persona not in domain_member_keys(fold):
        raise CredentialError(
            "credential persona is not a current content-holding domain member"
        )
    return credential


def select_current_credential(candidates, ancestry) -> PersonaKemCredential:
    """The one current credential among *candidates* (one persona).

    A credential whose cited frontier strictly causally descends from
    another's supersedes it; equal or incomparable frontiers are
    concurrent and resolve by ascending ``kem_key_id`` with the greatest
    current (contract §5, matching the ledger's ascending-identifier
    merge order). *ancestry* is the causal-closure seam
    (``Ledger.ancestry``): heads -> inclusive ancestor id set. The caller
    pre-filters retired signing keys via :func:`verify_against_fold`.
    """
    unique = {c.kem_key_id: c for c in candidates}
    if not unique:
        raise CredentialError("no candidate credentials")
    creds = list(unique.values())
    closures = {c.kem_key_id: frozenset(ancestry(c.authority_heads)) for c in creds}

    def strictly_descends(a, b) -> bool:
        # b's frontier inside a's closure, but not vice versa: equal
        # frontiers are concurrent, not mutually superseding.
        return frozenset(b.authority_heads) <= closures[a.kem_key_id] and not (
            frozenset(a.authority_heads) <= closures[b.kem_key_id]
        )

    frontier = [
        b for b in creds if not any(strictly_descends(a, b) for a in creds if a is not b)
    ]
    return max(frontier, key=lambda c: c.kem_key_id)
