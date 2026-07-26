"""Storage-state descriptor: a state secret's signed public record.

A storage state is a fresh uniformly random 256-bit secret; its
descriptor is the Ed25519-signed public record every parent bridge,
capability grant, and object-key header names (contract §7). The
descriptor commits to the secret — via a labelled SHA-256 commitment
over the organization, domain, and nonce — but NEVER carries it; the raw
secret travels only inside sealed capability grants.

The descriptor cites the authority frontier it was created at
(``authority_heads``) and the access-loss projection there
(``covered_loss_heads``, ``loss_projection_digest``), carried verbatim
from the fold's projection. Recomputing and judging that citation is the
acceptance-procedures bead's concern; :func:`verify_structure` covers
what is checkable without the fold.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, fields, replace

from tools.network.idkit import KeyPair, canonical_json, verify_signature
from tools.network.idkit.errors import IdkitError

from . import suites
from .errors import CommitmentError, MalformedRecordError, RecordSignatureError
from .records import parse_canonical, record_id, signing_input

STATE_VERSION = 1
STATE_DOMAIN = b"autonomy.storage.state.v1\n"
STATE_SECRET_LABEL = b"autonomy/storage-state-secret/v1"
SUITE_ID = suites.WRAP_SUITE
STATE_SECRET_LEN = 32

_ID_HEX_LEN = 64  # SHA-256 hex: genesis/domain/state ids, digests, nonce
_KEY_HEX_LEN = 64  # Ed25519 public key hex
_SIG_HEX_LEN = 128


def _require_hex(value: object, length: int, what: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or value != value.lower()
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise MalformedRecordError(f"{what} must be {length} lowercase hex chars")
    return value


def _require_id_tuple(value: object, what: str) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise MalformedRecordError(f"{what} must be a list of identifiers")
    for entry in value:
        _require_hex(entry, _ID_HEX_LEN, f"{what} entry")
    if list(value) != sorted(set(value)):
        raise MalformedRecordError(f"{what} must be sorted and free of duplicates")
    return tuple(value)


@dataclass(frozen=True)
class StorageStateDescriptor:
    version: int
    suite_id: str
    genesis_id: str
    domain_id: str
    state_nonce: str
    parent_state_ids: tuple
    authority_heads: tuple
    covered_loss_heads: tuple
    loss_projection_digest: str
    secret_commitment: str
    creator_persona: str
    signature: str

    def signed_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        del d["signature"]
        for name in ("parent_state_ids", "authority_heads", "covered_loss_heads"):
            d[name] = list(d[name])
        return d

    def signing_input(self) -> bytes:
        return signing_input(STATE_DOMAIN, self.signed_dict())

    def to_json(self) -> bytes:
        """The canonical wire: the full record including the signature."""
        return canonical_json({**self.signed_dict(), "signature": self.signature})

    @property
    def state_id(self) -> str:
        """SHA-256 of the signed wire — commits to every field."""
        return record_id(self.to_json())

    @classmethod
    def from_json(cls, wire: bytes) -> "StorageStateDescriptor":
        """Strict parse + full structural verification. One byte form only."""
        data = parse_canonical(_FIELDS, wire)
        for name in ("parent_state_ids", "authority_heads", "covered_loss_heads"):
            if not isinstance(data[name], list):
                raise MalformedRecordError(f"{name} must be a list")
            data[name] = tuple(data[name])
        descriptor = cls(**data)
        verify_structure(descriptor)
        return descriptor


_FIELDS = tuple(f.name for f in fields(StorageStateDescriptor))


def compute_secret_commitment(
    genesis_id: str, domain_id: str, state_nonce: str, state_secret: bytes
) -> str:
    """The labelled commitment binding the secret to org, domain, and nonce.

    The identifiers are fixed-length lowercase-hex ASCII and the secret is
    32 raw bytes, so the concatenation boundaries are unambiguous.
    """
    _require_hex(genesis_id, _ID_HEX_LEN, "genesis_id")
    _require_hex(domain_id, _ID_HEX_LEN, "domain_id")
    _require_hex(state_nonce, _ID_HEX_LEN, "state_nonce")
    if not isinstance(state_secret, bytes) or len(state_secret) != STATE_SECRET_LEN:
        raise MalformedRecordError(f"state secret must be {STATE_SECRET_LEN} raw bytes")
    return hashlib.sha256(
        STATE_SECRET_LABEL
        + genesis_id.encode("ascii")
        + domain_id.encode("ascii")
        + state_nonce.encode("ascii")
        + state_secret
    ).hexdigest()


def generate(
    creator: KeyPair,
    genesis_id: str,
    domain_id: str,
    parent_state_ids,
    authority_heads,
    covered_loss_heads,
    loss_projection_digest: str,
) -> tuple:
    """Mint a fresh state: returns ``(descriptor, state_secret)``.

    The secret and nonce come from the OS CSPRNG. The raw secret is never
    a descriptor field — deliver it only inside sealed capability grants.
    ``covered_loss_heads`` and ``loss_projection_digest`` are the
    access-loss projection's outputs, carried verbatim.
    """
    state_secret = os.urandom(STATE_SECRET_LEN)
    state_nonce = os.urandom(32).hex()
    descriptor = StorageStateDescriptor(
        version=STATE_VERSION,
        suite_id=SUITE_ID,
        genesis_id=_require_hex(genesis_id, _ID_HEX_LEN, "genesis_id"),
        domain_id=_require_hex(domain_id, _ID_HEX_LEN, "domain_id"),
        state_nonce=state_nonce,
        parent_state_ids=_require_id_tuple(
            sorted(set(parent_state_ids)), "parent_state_ids"
        ),
        authority_heads=_require_id_tuple(sorted(set(authority_heads)), "authority_heads"),
        covered_loss_heads=_require_id_tuple(
            sorted(set(covered_loss_heads)), "covered_loss_heads"
        ),
        loss_projection_digest=_require_hex(
            loss_projection_digest, _ID_HEX_LEN, "loss_projection_digest"
        ),
        secret_commitment=compute_secret_commitment(
            genesis_id, domain_id, state_nonce, state_secret
        ),
        creator_persona=creator.public_hex,
        signature="0" * _SIG_HEX_LEN,
    )
    signed = replace(descriptor, signature=creator.sign_hex(descriptor.signing_input()))
    return signed, state_secret


def verify_structure(descriptor: StorageStateDescriptor) -> None:
    """Every acceptance check performable without the fold (contract §9).

    Version, suite recognition (fails closed), field forms, identifier-
    list ordering, and the creator signature. The cited frontier and the
    projection recomputation belong to the acceptance-procedures layer.
    """
    if descriptor.version != STATE_VERSION:
        raise MalformedRecordError(f"unsupported descriptor version: {descriptor.version!r}")
    suites.require_suite(descriptor.suite_id, suites.WRAP_SUITES)
    _require_hex(descriptor.genesis_id, _ID_HEX_LEN, "genesis_id")
    _require_hex(descriptor.domain_id, _ID_HEX_LEN, "domain_id")
    _require_hex(descriptor.state_nonce, _ID_HEX_LEN, "state_nonce")
    _require_id_tuple(descriptor.parent_state_ids, "parent_state_ids")
    _require_id_tuple(descriptor.authority_heads, "authority_heads")
    _require_id_tuple(descriptor.covered_loss_heads, "covered_loss_heads")
    _require_hex(descriptor.loss_projection_digest, _ID_HEX_LEN, "loss_projection_digest")
    _require_hex(descriptor.secret_commitment, _ID_HEX_LEN, "secret_commitment")
    _require_hex(descriptor.creator_persona, _KEY_HEX_LEN, "creator_persona")
    _require_hex(descriptor.signature, _SIG_HEX_LEN, "signature")
    try:
        verify_signature(
            descriptor.creator_persona, descriptor.signature, descriptor.signing_input()
        )
    except IdkitError as exc:
        raise RecordSignatureError("descriptor signature does not verify") from exc


def verify_secret_commitment(descriptor: StorageStateDescriptor, state_secret: bytes) -> None:
    """Constant-time check of *state_secret* against the commitment."""
    expected = compute_secret_commitment(
        descriptor.genesis_id,
        descriptor.domain_id,
        descriptor.state_nonce,
        state_secret,
    )
    if not hmac.compare_digest(expected, descriptor.secret_commitment):
        raise CommitmentError("state secret does not match the descriptor commitment")
