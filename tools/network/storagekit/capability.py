"""Capability grant and receipt — state-secret delivery and proof of possession.

The **grant** delivers one storage-state secret to one recipient persona
by sealing it (hybrid public-key encryption via the idkit sealing
primitive) to that persona's key-encapsulation credential. The sealing
purpose binds the organization, domain, state, and recipient credential
identifiers into the encryption context — recomputed from the grant's
own fields at both ends — so a grant replayed into any other context
fails to open. It carries no content body and no historical key set: a
holder of one state secret reaches ancestors through parent bridges.

The **receipt** is a persona's signed proof of possession of a state
secret (contract §7): its possession tag is HKDF-Expanded *from the
secret itself* over the four identifiers, so it is computable only with
the secret and verifiable by any other holder — a proof of knowledge,
not an assertion. Receipts feed the distribution bead's per-branch
commit status (§10); the creator's self-receipt counts as one.

Fold-based admissibility — grantor and recipient membership, credential
currency, frontier recency over the descriptor's ``authority_heads`` —
is the acceptance-procedures bead's ``accept_grant``, run before
:func:`accept` on every honest node.
"""

from __future__ import annotations

import hmac as hmac_mod
from dataclasses import dataclass, fields, replace

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit import verify_signature as idkit_verify_signature
from tools.network.idkit.errors import IdkitError
from tools.network.idkit.sealing import open as seal_open
from tools.network.idkit.sealing import seal

from . import suites
from .errors import CommitmentError, MalformedRecordError, RecordSignatureError, StorageError
from .records import parse_canonical, record_id, signing_input
from .state import STATE_SECRET_LEN, compute_secret_commitment
from .state import _require_hex, _require_id_tuple

CAPABILITY_GRANT_VERSION = 1
CAPABILITY_RECEIPT_VERSION = 1
CAPABILITY_GRANT_DOMAIN = b"autonomy.storage.capability-grant.v1\n"
CAPABILITY_RECEIPT_DOMAIN = b"autonomy.storage.capability-receipt.v1\n"
RECEIPT_POK_LABEL = b"autonomy/receipt-pok/v1"
GRANT_PURPOSE_PREFIX = "autonomy/capability-grant/v1"

_ID_HEX_LEN = 64
_KEY_HEX_LEN = 64
_SIG_HEX_LEN = 128
#: Sealed 32-byte secret under the v1 suite: 1 suite byte + 32 enc + 48 ct.
_SEALED_SECRET_HEX_LEN = 2 * (1 + 32 + STATE_SECRET_LEN + 16)


class PossessionTagError(StorageError):
    """A receipt's possession tag does not verify against the state secret."""


def grant_purpose(
    genesis_id: str, domain_id: str, storage_state_id: str, recipient_kem_key_id: str
) -> str:
    """The sealing purpose label — fixed-length hex segments, unambiguous."""
    return "/".join(
        (
            GRANT_PURPOSE_PREFIX,
            _require_hex(genesis_id, _ID_HEX_LEN, "genesis_id"),
            _require_hex(domain_id, _ID_HEX_LEN, "domain_id"),
            _require_hex(storage_state_id, _ID_HEX_LEN, "storage_state_id"),
            _require_hex(recipient_kem_key_id, _ID_HEX_LEN, "recipient_kem_key_id"),
        )
    )


def receipt_possession_tag(
    state_secret: bytes,
    genesis_id: str,
    domain_id: str,
    storage_state_id: str,
    receiver_persona: str,
) -> str:
    """Contract §7: HKDF-Expand(state_secret, label || the four ids, 32)."""
    if not isinstance(state_secret, bytes) or len(state_secret) != STATE_SECRET_LEN:
        raise MalformedRecordError(f"state secret must be {STATE_SECRET_LEN} raw bytes")
    info = (
        RECEIPT_POK_LABEL
        + _require_hex(genesis_id, _ID_HEX_LEN, "genesis_id").encode("ascii")
        + _require_hex(domain_id, _ID_HEX_LEN, "domain_id").encode("ascii")
        + _require_hex(storage_state_id, _ID_HEX_LEN, "storage_state_id").encode("ascii")
        + _require_hex(receiver_persona, _KEY_HEX_LEN, "receiver_persona").encode("ascii")
    )
    return HKDFExpand(algorithm=hashes.SHA256(), length=32, info=info).derive(
        state_secret
    ).hex()


# -- records ------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityGrant:
    version: int
    hpke_suite_id: int
    genesis_id: str
    domain_id: str
    storage_state_id: str
    recipient_kem_key_id: str
    authority_heads: tuple
    encapsulated_state_secret: str  # hex of the sealed wire record
    state_secret_commitment: str
    grantor_persona: str
    signature: str

    def signed_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        del d["signature"]
        d["authority_heads"] = list(d["authority_heads"])
        return d

    def signing_input(self) -> bytes:
        return signing_input(CAPABILITY_GRANT_DOMAIN, self.signed_dict())

    def to_json(self) -> bytes:
        return canonical_json({**self.signed_dict(), "signature": self.signature})

    @property
    def grant_id(self) -> str:
        return record_id(self.to_json())

    @classmethod
    def from_json(cls, wire: bytes) -> "CapabilityGrant":
        data = parse_canonical(_GRANT_FIELDS, wire)
        if not isinstance(data["authority_heads"], list):
            raise MalformedRecordError("authority_heads must be a list")
        data["authority_heads"] = tuple(data["authority_heads"])
        return cls(**data)


@dataclass(frozen=True)
class CapabilityReceipt:
    version: int
    genesis_id: str
    domain_id: str
    storage_state_id: str
    receiver_persona: str
    possession_tag: str
    signature: str

    def signed_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        del d["signature"]
        return d

    def signing_input(self) -> bytes:
        return signing_input(CAPABILITY_RECEIPT_DOMAIN, self.signed_dict())

    def to_json(self) -> bytes:
        return canonical_json({**self.signed_dict(), "signature": self.signature})

    @property
    def receipt_id(self) -> str:
        return record_id(self.to_json())

    @classmethod
    def from_json(cls, wire: bytes) -> "CapabilityReceipt":
        return cls(**parse_canonical(_RECEIPT_FIELDS, wire))


_GRANT_FIELDS = tuple(f.name for f in fields(CapabilityGrant))
_RECEIPT_FIELDS = tuple(f.name for f in fields(CapabilityReceipt))


def _check_grant(grant: CapabilityGrant) -> None:
    if grant.version != CAPABILITY_GRANT_VERSION:
        raise MalformedRecordError(f"unsupported grant version: {grant.version!r}")
    suites.require_suite(grant.hpke_suite_id, suites.SEAL_SUITES)
    for name in ("genesis_id", "domain_id", "storage_state_id", "recipient_kem_key_id"):
        _require_hex(getattr(grant, name), _ID_HEX_LEN, name)
    _require_id_tuple(grant.authority_heads, "authority_heads")
    _require_hex(
        grant.encapsulated_state_secret, _SEALED_SECRET_HEX_LEN, "encapsulated_state_secret"
    )
    _require_hex(grant.state_secret_commitment, _ID_HEX_LEN, "state_secret_commitment")
    _require_hex(grant.grantor_persona, _KEY_HEX_LEN, "grantor_persona")
    _require_hex(grant.signature, _SIG_HEX_LEN, "signature")


def _check_receipt(receipt: CapabilityReceipt) -> None:
    if receipt.version != CAPABILITY_RECEIPT_VERSION:
        raise MalformedRecordError(f"unsupported receipt version: {receipt.version!r}")
    for name in ("genesis_id", "domain_id", "storage_state_id"):
        _require_hex(getattr(receipt, name), _ID_HEX_LEN, name)
    _require_hex(receipt.receiver_persona, _KEY_HEX_LEN, "receiver_persona")
    _require_hex(receipt.possession_tag, _ID_HEX_LEN, "possession_tag")
    _require_hex(receipt.signature, _SIG_HEX_LEN, "signature")


def verify_grant(record) -> CapabilityGrant:
    """Foundation parse (for wire bytes) plus the grantor signature."""
    if isinstance(record, (bytes, bytearray)):
        grant = CapabilityGrant.from_json(bytes(record))
    elif isinstance(record, CapabilityGrant):
        grant = record
    else:
        raise MalformedRecordError("grant must be wire bytes or a CapabilityGrant")
    _check_grant(grant)
    try:
        idkit_verify_signature(grant.grantor_persona, grant.signature, grant.signing_input())
    except IdkitError as exc:
        raise RecordSignatureError("grant signature does not verify") from exc
    return grant


def verify_receipt(record) -> CapabilityReceipt:
    """Foundation parse (for wire bytes) plus the receiver signature."""
    if isinstance(record, (bytes, bytearray)):
        receipt = CapabilityReceipt.from_json(bytes(record))
    elif isinstance(record, CapabilityReceipt):
        receipt = record
    else:
        raise MalformedRecordError("receipt must be wire bytes or a CapabilityReceipt")
    _check_receipt(receipt)
    try:
        idkit_verify_signature(
            receipt.receiver_persona, receipt.signature, receipt.signing_input()
        )
    except IdkitError as exc:
        raise RecordSignatureError("receipt signature does not verify") from exc
    return receipt


# -- grant lifecycle ------------------------------------------------------------------


def issue(
    grantor: KeyPair,
    *,
    genesis_id: str,
    domain_id: str,
    storage_state_id: str,
    recipient_credential,
    state_secret: bytes,
    state_secret_commitment: str,
    authority_heads,
) -> CapabilityGrant:
    """Seal one state secret to one recipient credential and sign."""
    if not isinstance(state_secret, bytes) or len(state_secret) != STATE_SECRET_LEN:
        raise MalformedRecordError(f"state secret must be {STATE_SECRET_LEN} raw bytes")
    purpose = grant_purpose(
        genesis_id, domain_id, storage_state_id, recipient_credential.kem_key_id
    )
    sealed = seal(
        state_secret, recipient_credential.kem_public_key, purpose, suites.SEAL_SUITE
    )
    unsigned = CapabilityGrant(
        version=CAPABILITY_GRANT_VERSION,
        hpke_suite_id=suites.SEAL_SUITE,
        genesis_id=genesis_id,
        domain_id=domain_id,
        storage_state_id=storage_state_id,
        recipient_kem_key_id=recipient_credential.kem_key_id,
        authority_heads=_require_id_tuple(sorted(set(authority_heads)), "authority_heads"),
        encapsulated_state_secret=sealed.hex(),
        state_secret_commitment=_require_hex(
            state_secret_commitment, _ID_HEX_LEN, "state_secret_commitment"
        ),
        grantor_persona=grantor.public_hex,
        signature="0" * _SIG_HEX_LEN,
    )
    return replace(unsigned, signature=grantor.sign_hex(unsigned.signing_input()))


def accept(grant, recipient_kem_private_key: str, state_descriptor) -> bytes:
    """Recover the state secret; every check must pass before it returns.

    Wire + signature verification, suite fail-closed before any
    decapsulation, descriptor context equality, purpose-bound
    decapsulation (wrong key, tamper, or foreign context fails in the
    sealing primitive), and the double commitment check — the recovered
    secret must match both the grant's carried commitment and the
    descriptor's (Invariant 9). Fold admissibility ran before this call.
    """
    grant = verify_grant(grant)
    suites.require_suite(grant.hpke_suite_id, suites.SEAL_SUITES)
    if (
        grant.genesis_id != state_descriptor.genesis_id
        or grant.domain_id != state_descriptor.domain_id
        or grant.storage_state_id != state_descriptor.state_id
    ):
        raise MalformedRecordError("grant does not reference the presented descriptor")
    purpose = grant_purpose(
        grant.genesis_id,
        grant.domain_id,
        grant.storage_state_id,
        grant.recipient_kem_key_id,
    )
    secret = seal_open(
        bytes.fromhex(grant.encapsulated_state_secret), recipient_kem_private_key, purpose
    )
    if len(secret) != STATE_SECRET_LEN:
        raise MalformedRecordError("grant plaintext is not a state secret")
    expected = compute_secret_commitment(
        state_descriptor.genesis_id,
        state_descriptor.domain_id,
        state_descriptor.state_nonce,
        secret,
    )
    if not (
        hmac_mod.compare_digest(expected, grant.state_secret_commitment)
        and hmac_mod.compare_digest(expected, state_descriptor.secret_commitment)
    ):
        raise CommitmentError(
            "recovered secret does not match the grant and descriptor commitments"
        )
    return secret


# -- receipt lifecycle ----------------------------------------------------------------


def issue_receipt(
    receiver: KeyPair,
    *,
    genesis_id: str,
    domain_id: str,
    storage_state_id: str,
    state_secret: bytes,
) -> CapabilityReceipt:
    """Signed proof of possession — issued in the same act as a successful
    accept (or, for the creator, as descriptor creation)."""
    unsigned = CapabilityReceipt(
        version=CAPABILITY_RECEIPT_VERSION,
        genesis_id=genesis_id,
        domain_id=domain_id,
        storage_state_id=storage_state_id,
        receiver_persona=receiver.public_hex,
        possession_tag=receipt_possession_tag(
            state_secret, genesis_id, domain_id, storage_state_id, receiver.public_hex
        ),
        signature="0" * _SIG_HEX_LEN,
    )
    return replace(unsigned, signature=receiver.sign_hex(unsigned.signing_input()))


def verify_possession_tag(receipt: CapabilityReceipt, state_secret: bytes) -> None:
    """Constant-time recompute-and-compare, runnable by any secret holder."""
    expected = receipt_possession_tag(
        state_secret,
        receipt.genesis_id,
        receipt.domain_id,
        receipt.storage_state_id,
        receipt.receiver_persona,
    )
    if not hmac_mod.compare_digest(expected, receipt.possession_tag):
        raise PossessionTagError(
            "possession tag does not verify for this secret and identifier tuple"
        )
