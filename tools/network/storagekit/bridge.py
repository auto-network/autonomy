"""Parent-secret bridge: backward, authenticated state-secret recovery.

A bridge is one directed edge of the storage-state DAG: the parent
state's secret, AEAD-encrypted under a key derived from the *child*
state's secret (contract §7). Holding a child secret therefore reveals
every reachable ancestor secret through the bridge chain; holding a
parent secret reveals no child secret (Invariant 4) — the edge key is
HKDF-Expand over the child secret, and nothing about a child is
derivable from a parent.

Edge key and associated data are recomputed identically at create and
open from the record's own context fields, so a bridge replayed under a
different organization, domain, child, or parent fails authentication.
Every recovered secret is checked against its descriptor's commitment
before it is returned (Invariant 9): no path returns an unverified
secret.

Wire acceptance is ``from_json`` (anti-malleable, structural) plus
:func:`verify_signature`; the fold-based issuer-authority check belongs
to the acceptance-procedures layer.
"""

from __future__ import annotations

import base64
import binascii
import hmac as hmac_mod
import os
from dataclasses import dataclass, fields, replace

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit import verify_signature as idkit_verify_signature
from tools.network.idkit.errors import IdkitError

from . import suites
from .errors import CommitmentError, MalformedRecordError, RecordSignatureError, StorageError
from .records import parse_canonical, record_id, signing_input
from .state import STATE_SECRET_LEN, compute_secret_commitment
from .state import _require_hex, _require_id_tuple  # shared field validators

BRIDGE_VERSION = 1
BRIDGE_DOMAIN = b"autonomy.storage.parent-bridge.v1\n"
BRIDGE_AAD_DOMAIN = b"autonomy.storage.parent-bridge.aad.v1\n"
EDGE_INFO_LABEL = b"autonomy/storage-parent-edge/v1"
SUITE_ID = suites.WRAP_SUITE
NONCE_LEN = 12

_ID_HEX_LEN = 64
_KEY_HEX_LEN = 64
_SIG_HEX_LEN = 128
_CT_LEN = STATE_SECRET_LEN + 16  # AEAD output: secret + tag


class BridgeError(StorageError):
    """The bridge does not open — wrong child secret or altered context."""


def _require_b64(value: object, length: int, what: str) -> bytes:
    if not isinstance(value, str):
        raise MalformedRecordError(f"{what} must be a base64 string")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MalformedRecordError(f"{what} does not base64-decode") from exc
    if len(raw) != length:
        raise MalformedRecordError(f"{what} must decode to exactly {length} bytes")
    if base64.b64encode(raw).decode("ascii") != value:
        raise MalformedRecordError(f"{what} is not canonical base64")
    return raw


def _require_secret(value: object, what: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != STATE_SECRET_LEN:
        raise MalformedRecordError(f"{what} must be {STATE_SECRET_LEN} raw bytes")
    return value


@dataclass(frozen=True)
class ParentBridge:
    version: int
    suite_id: str
    genesis_id: str
    domain_id: str
    child_state_id: str
    parent_state_id: str
    nonce: str
    encrypted_parent_secret: str
    authority_heads: tuple
    issuer_persona: str
    signature: str

    def signed_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        del d["signature"]
        d["authority_heads"] = list(d["authority_heads"])
        return d

    def signing_input(self) -> bytes:
        return signing_input(BRIDGE_DOMAIN, self.signed_dict())

    def to_json(self) -> bytes:
        return canonical_json({**self.signed_dict(), "signature": self.signature})

    @property
    def bridge_id(self) -> str:
        return record_id(self.to_json())

    @classmethod
    def from_json(cls, wire: bytes) -> "ParentBridge":
        data = parse_canonical(_FIELDS, wire)
        if not isinstance(data["authority_heads"], list):
            raise MalformedRecordError("authority_heads must be a list")
        data["authority_heads"] = tuple(data["authority_heads"])
        bridge = cls(**data)
        _check_structure(bridge)
        return bridge


_FIELDS = tuple(f.name for f in fields(ParentBridge))


def _check_structure(bridge: ParentBridge) -> None:
    if bridge.version != BRIDGE_VERSION:
        raise MalformedRecordError(f"unsupported bridge version: {bridge.version!r}")
    suites.require_suite(bridge.suite_id, suites.WRAP_SUITES)
    _require_hex(bridge.genesis_id, _ID_HEX_LEN, "genesis_id")
    _require_hex(bridge.domain_id, _ID_HEX_LEN, "domain_id")
    _require_hex(bridge.child_state_id, _ID_HEX_LEN, "child_state_id")
    _require_hex(bridge.parent_state_id, _ID_HEX_LEN, "parent_state_id")
    _require_b64(bridge.nonce, NONCE_LEN, "nonce")
    _require_b64(bridge.encrypted_parent_secret, _CT_LEN, "encrypted_parent_secret")
    _require_id_tuple(bridge.authority_heads, "authority_heads")
    _require_hex(bridge.issuer_persona, _KEY_HEX_LEN, "issuer_persona")
    _require_hex(bridge.signature, _SIG_HEX_LEN, "signature")


def _edge_context(bridge: ParentBridge) -> tuple:
    """(edge-key info, associated data) — identical at create and open."""
    aad = BRIDGE_AAD_DOMAIN + canonical_json(
        {
            "version": bridge.version,
            "suite_id": bridge.suite_id,
            "genesis_id": bridge.genesis_id,
            "domain_id": bridge.domain_id,
            "child_state_id": bridge.child_state_id,
            "parent_state_id": bridge.parent_state_id,
        }
    )
    info = EDGE_INFO_LABEL + canonical_json(
        [bridge.genesis_id, bridge.domain_id, bridge.child_state_id, bridge.parent_state_id]
    )
    return info, aad


def _edge_key(info: bytes, child_state_secret: bytes) -> bytes:
    # The child secret is uniformly random 256-bit, so expand-only
    # derivation with it as the PRK is the specified construction.
    return HKDFExpand(algorithm=hashes.SHA256(), length=32, info=info).derive(
        child_state_secret
    )


def create(
    issuer: KeyPair,
    *,
    genesis_id: str,
    domain_id: str,
    child_state_id: str,
    parent_state_id: str,
    child_state_secret: bytes,
    parent_state_secret: bytes,
    authority_heads,
) -> ParentBridge:
    """Mint the signed bridge carrying parent's secret under child's key."""
    _require_secret(child_state_secret, "child_state_secret")
    _require_secret(parent_state_secret, "parent_state_secret")
    nonce = os.urandom(NONCE_LEN)
    unsigned = ParentBridge(
        version=BRIDGE_VERSION,
        suite_id=SUITE_ID,
        genesis_id=_require_hex(genesis_id, _ID_HEX_LEN, "genesis_id"),
        domain_id=_require_hex(domain_id, _ID_HEX_LEN, "domain_id"),
        child_state_id=_require_hex(child_state_id, _ID_HEX_LEN, "child_state_id"),
        parent_state_id=_require_hex(parent_state_id, _ID_HEX_LEN, "parent_state_id"),
        nonce=base64.b64encode(nonce).decode("ascii"),
        encrypted_parent_secret="",
        authority_heads=_require_id_tuple(sorted(set(authority_heads)), "authority_heads"),
        issuer_persona=issuer.public_hex,
        signature="0" * _SIG_HEX_LEN,
    )
    info, aad = _edge_context(unsigned)
    ct = AESGCMSIV(_edge_key(info, child_state_secret)).encrypt(
        nonce, parent_state_secret, aad
    )
    sealed = replace(unsigned, encrypted_parent_secret=base64.b64encode(ct).decode("ascii"))
    return replace(sealed, signature=issuer.sign_hex(sealed.signing_input()))


def open(bridge: ParentBridge, child_state_secret: bytes) -> bytes:  # noqa: A001
    """Recover the parent secret with the child secret; fails closed."""
    _require_secret(child_state_secret, "child_state_secret")
    if bridge.version != BRIDGE_VERSION:
        raise MalformedRecordError(f"unsupported bridge version: {bridge.version!r}")
    suites.require_suite(bridge.suite_id, suites.WRAP_SUITES)
    nonce = _require_b64(bridge.nonce, NONCE_LEN, "nonce")
    ct = _require_b64(bridge.encrypted_parent_secret, _CT_LEN, "encrypted_parent_secret")
    info, aad = _edge_context(bridge)
    try:
        secret = AESGCMSIV(_edge_key(info, child_state_secret)).decrypt(nonce, ct, aad)
    except (InvalidTag, ValueError) as exc:
        raise BridgeError("bridge does not open with that child secret") from exc
    if len(secret) != STATE_SECRET_LEN:
        raise BridgeError("bridge plaintext is not a state secret")
    return secret


def verify_signature(bridge: ParentBridge) -> None:
    """Verify the issuer signature over the signed fields."""
    _check_structure(bridge)
    try:
        idkit_verify_signature(bridge.issuer_persona, bridge.signature, bridge.signing_input())
    except IdkitError as exc:
        raise RecordSignatureError("bridge signature does not verify") from exc


def recover_ancestors(
    held_state_id: str,
    held_state_secret: bytes,
    bridges,
    descriptors,
) -> dict:
    """Every reachable ancestor secret from a held state, via bridges.

    Breadth-first over signature-verified bridges (any bridge that fails
    verification raises — fail loud, not silent skip). Each recovered
    secret is checked against its parent descriptor's commitment before
    it is returned or followed (Invariant 9); a mismatch raises
    :class:`CommitmentError` naming the parent, so a poisoned bridge
    never contributes a secret or a subtree. Returns
    ``{state_id: secret}`` including the seed.
    """
    _require_hex(held_state_id, _ID_HEX_LEN, "held_state_id")
    _require_secret(held_state_secret, "held_state_secret")
    by_child: dict = {}
    for bridge in sorted(bridges, key=lambda b: b.bridge_id):
        verify_signature(bridge)
        by_child.setdefault(bridge.child_state_id, []).append(bridge)

    recovered = {held_state_id: held_state_secret}
    frontier = [held_state_id]
    while frontier:
        state_id = frontier.pop(0)
        for bridge in by_child.get(state_id, ()):
            parent_id = bridge.parent_state_id
            if parent_id in recovered:
                continue
            secret = open(bridge, recovered[state_id])
            descriptor = descriptors.get(parent_id)
            if descriptor is None:
                raise MalformedRecordError(
                    f"no descriptor for parent state {parent_id}"
                )
            expected = compute_secret_commitment(
                descriptor.genesis_id,
                descriptor.domain_id,
                descriptor.state_nonce,
                secret,
            )
            if not hmac_mod.compare_digest(expected, descriptor.secret_commitment):
                raise CommitmentError(
                    f"recovered secret for parent state {parent_id} does not match "
                    "its descriptor commitment"
                )
            recovered[parent_id] = secret
            frontier.append(parent_id)
    return recovered
