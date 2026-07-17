"""Equivocation witness — signed, append-only head-set attestations (L5).

Spec ``graph://eb245082-b76`` §6 role 2, bead ``auto-12jah`` (F4). The
witness is the registry's **T1-neutral anti-fork role**. It keeps a
per-``(org, topic)`` *append-only, hash-chained* log of the head-sets
members publish, and serves each tip **signed by the registry's witness
key**. Because every served attestation is non-repudiably signed and
carries its chain position (``seq`` + ``prev``), two members shown
*different* histories each hold a signed statement, and the contradiction
between the two is a self-contained, independently-checkable proof of
misbehaviour — Certificate-Transparency split-view detection, reduced to
hashes so it composes with L6 encryption (the witness never sees an event,
only its id).

This module owns the **wire format** and the **signing/verification**
primitives — shared, so the server that mints an attestation and the
:mod:`ledger.witness` client that checks one agree on one byte form. It is
the registry's counterpart of :mod:`ledger.witness`, the same way
:mod:`registry.signing` is the counterpart of the broker client.

Wire shapes
-----------
An *entry* is the canonical, hashes-only unit the chain is built from::

    entry = {"org", "topic", "seq", "heads": [sorted ids],
             "prev": <entry_id | null>, "publisher": <pubkey>}
    entry_id = sha256(canonical_json(entry))           # its content address

A served *attestation* is an entry plus the witness signature over it::

    {"entry": {...}, "entry_id": <hex>, "sig": <128 hex>, "witness_pub": <64 hex>}
    sig = Ed25519(witness_priv, WITNESS_DOMAIN || canonical_json(entry))

The signature — not the transport — is what makes an attestation
*evidence*: anyone holding two attestations for the same ``(org, topic,
seq)`` with different ``entry_id``, both verifying under the *same* pinned
witness key, holds a proof the witness equivocated. Domain separation
(``WITNESS_DOMAIN``) keeps a witness signature from ever being mistaken
for a request envelope, cert, or revocation signature.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional

from tools.network.idkit import KeyPair, MalformedError, canonical_json, verify_signature
from tools.network.idkit.keys import (
    PUBLIC_KEY_HEX_LEN,
    SIGNATURE_HEX_LEN,
    _decode_hex,
)

WITNESS_DOMAIN = b"autonomy.network.registry.witness.v1\n"
WITNESS_VERSION = 1

#: Head-set cap, shared with the hint surface (:data:`app.MAX_HINT_HEADS`).
MAX_WITNESS_HEADS = 64

_ENTRY_FIELDS = ("org", "topic", "seq", "heads", "prev", "publisher")


class WitnessFormatError(MalformedError):
    """An attestation entry is structurally malformed."""


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(c in "0123456789abcdef" for c in value)
    )


def build_entry(
    org: str,
    topic: str,
    seq: int,
    heads: List[str],
    prev: Optional[str],
    publisher: str,
) -> dict:
    """Assemble a witness log entry dict (validated, canonical field set).

    ``heads`` is stored sorted and de-duplicated so the entry — and hence
    its content-address :func:`entry_id` — is a pure function of the head
    *set*, independent of the order the publisher happened to list them.
    """
    if not isinstance(seq, int) or seq < 1:
        raise WitnessFormatError("witness entry seq must be a positive integer")
    if not isinstance(heads, list) or not heads or len(heads) > MAX_WITNESS_HEADS:
        raise WitnessFormatError(
            f"witness entry heads must be 1..{MAX_WITNESS_HEADS} event ids"
        )
    for h in heads:
        if not _is_hash(h):
            raise WitnessFormatError("witness entry heads must be 64-char lowercase hex")
    if prev is not None and not _is_hash(prev):
        raise WitnessFormatError("witness entry prev must be null or a 64-char hex id")
    if (seq == 1) != (prev is None):
        raise WitnessFormatError("witness entry prev is null iff seq == 1")
    if not _is_hash(publisher):
        raise WitnessFormatError("witness entry publisher must be a 64-char hex pubkey")
    if not isinstance(org, str) or not org or not isinstance(topic, str) or not topic:
        raise WitnessFormatError("witness entry org and topic must be non-empty strings")
    return {
        "org": org,
        "topic": topic,
        "seq": seq,
        "heads": sorted(set(heads)),
        "prev": prev,
        "publisher": publisher,
    }


def validate_entry(entry: object) -> dict:
    """Re-validate a received entry, returning it in canonical form.

    Rejects unknown/missing fields and any non-canonical ``heads`` order
    so a peer cannot smuggle two byte forms of "the same" entry (which
    would give two content addresses and defeat chain matching).
    """
    if not isinstance(entry, dict):
        raise WitnessFormatError("witness entry must be a JSON object")
    if set(entry) != set(_ENTRY_FIELDS):
        raise WitnessFormatError(f"witness entry must carry exactly {list(_ENTRY_FIELDS)}")
    rebuilt = build_entry(
        entry["org"], entry["topic"], entry["seq"],
        entry["heads"], entry["prev"], entry["publisher"],
    )
    if entry["heads"] != rebuilt["heads"]:
        raise WitnessFormatError("witness entry heads must be sorted and duplicate-free")
    return rebuilt


def entry_id(entry: dict) -> str:
    """Content address of an entry: sha256 of its canonical JSON."""
    return hashlib.sha256(canonical_json(entry)).hexdigest()


def attestation_signing_input(entry: dict) -> bytes:
    """The exact bytes a witness signature covers (domain-separated)."""
    return WITNESS_DOMAIN + canonical_json(entry)


def sign_attestation(witness_key: KeyPair, entry: dict) -> dict:
    """Mint a served attestation: the entry, its id, the witness signature.

    The returned ``witness_pub`` is advisory — a client must verify against
    a *pinned* witness key, never against the key the response names, or a
    split-view server could just sign each half under a different key.
    """
    return {
        "entry": entry,
        "entry_id": entry_id(entry),
        "sig": witness_key.sign_hex(attestation_signing_input(entry)),
        "witness_pub": witness_key.public_hex,
    }


def verify_attestation(attestation: object, witness_pub: str) -> dict:
    """Verify a served attestation against a **pinned** witness key.

    Returns the canonical entry on success. Raises
    :class:`WitnessFormatError` on a malformed shape or a signature that
    does not verify under *witness_pub* — the pin is the whole point, so a
    caller passes the key it trusts, not the one the blob advertises.
    """
    if not isinstance(attestation, dict):
        raise WitnessFormatError("attestation must be a JSON object")
    if set(attestation) < {"entry", "sig"}:
        raise WitnessFormatError("attestation must carry at least entry and sig")
    entry = validate_entry(attestation["entry"])
    sig = attestation.get("sig")
    if not isinstance(sig, str) or len(sig) != SIGNATURE_HEX_LEN:
        raise WitnessFormatError("attestation sig must be a 128-char hex signature")
    _decode_hex(witness_pub, PUBLIC_KEY_HEX_LEN, "witness_pub")
    try:
        verify_signature(witness_pub, sig, attestation_signing_input(entry))
    except Exception as exc:  # SignatureError / MalformedError from idkit
        raise WitnessFormatError(
            "attestation signature does not verify under the pinned witness key"
        ) from exc
    got = attestation.get("entry_id")
    if got is not None and got != entry_id(entry):
        raise WitnessFormatError("attestation entry_id does not match its entry")
    return entry
