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

#: Version 2 (auto-jqd9q): one hash-chained journal per org, whose entry
#: carries a signed non-decreasing ``t`` and heads grouped by topic, so a single
#: chain attests a consistent (authority frontier, key-control frontier) pair.
#: The v1 constant is retained to verify archived v1 chains.
WITNESS_DOMAIN_V2 = b"autonomy.network.registry.witness.v2\n"
WITNESS_VERSION_2 = 2

#: The closed topic set a v2 entry's ``heads`` map may key on. Only key-control
#: records (states, bridges, grants, receipts) contribute ``storage`` heads;
#: object headers never do.
WITNESS_TOPICS = ("authority", "storage")

#: Head-set cap, shared with the hint surface (:data:`app.MAX_HINT_HEADS`).
MAX_WITNESS_HEADS = 64

_ENTRY_FIELDS = ("org", "topic", "seq", "heads", "prev", "publisher")
_ENTRY_FIELDS_V2 = ("v", "org", "seq", "prev", "t", "heads", "publisher")


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
    """The exact bytes a witness signature covers (domain-separated).

    Dispatches on ``entry["v"]``: a v2 entry signs under ``WITNESS_DOMAIN_V2``,
    so a v1 signature can never be replayed as a v2 attestation or the reverse.
    A v1 entry carries no ``v`` field.
    """
    if entry.get("v") == WITNESS_VERSION_2:
        return WITNESS_DOMAIN_V2 + canonical_json(entry)
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
    return _verify_attestation_with(attestation, witness_pub, validate_entry)


def _verify_attestation_with(attestation, witness_pub, validator) -> dict:
    """Shared verify body, parameterised by the entry validator (v1 or v2).

    The signing input dispatches on ``entry["v"]`` inside
    :func:`attestation_signing_input`, so the domain is always the entry's own —
    the validator only decides which *shape* is accepted.
    """
    if not isinstance(attestation, dict):
        raise WitnessFormatError("attestation must be a JSON object")
    if set(attestation) < {"entry", "sig"}:
        raise WitnessFormatError("attestation must carry at least entry and sig")
    entry = validator(attestation["entry"])
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


# ── Version 2: one grouped, timestamped chain per org (auto-jqd9q) ────────────


def build_entry_v2(
    org: str,
    seq: int,
    heads_by_topic: dict,
    prev: Optional[str],
    t: int,
    publisher: str,
) -> dict:
    """Assemble a v2 witness entry (validated, canonical field set).

    ``heads_by_topic`` maps a topic in :data:`WITNESS_TOPICS` to its 1..
    ``MAX_WITNESS_HEADS`` head ids; each topic's ids are stored sorted and
    de-duplicated so the entry — and its :func:`entry_id` — is a pure function
    of the head *sets*. At least one topic must be present; a topic absent from
    an entry means "unchanged since the last entry that carried it". ``t`` is
    unix seconds, an integer ``>= 0``, and the chain enforces non-decrease.
    """
    if not isinstance(seq, int) or seq < 1:
        raise WitnessFormatError("witness entry seq must be a positive integer")
    if not isinstance(t, int) or isinstance(t, bool) or t < 0:
        raise WitnessFormatError("witness entry t must be a non-negative integer (unix seconds)")
    if not isinstance(heads_by_topic, dict) or not heads_by_topic:
        raise WitnessFormatError("witness entry heads must name at least one topic")
    canonical_heads: dict = {}
    for topic, heads in heads_by_topic.items():
        if topic not in WITNESS_TOPICS:
            raise WitnessFormatError(
                f"unknown witness topic {topic!r}; allowed: {list(WITNESS_TOPICS)}"
            )
        if not isinstance(heads, list) or not heads or len(heads) > MAX_WITNESS_HEADS:
            raise WitnessFormatError(
                f"witness topic {topic!r} heads must be 1..{MAX_WITNESS_HEADS} ids"
            )
        for h in heads:
            if not _is_hash(h):
                raise WitnessFormatError("witness entry heads must be 64-char lowercase hex")
        canonical_heads[topic] = sorted(set(heads))
    if prev is not None and not _is_hash(prev):
        raise WitnessFormatError("witness entry prev must be null or a 64-char hex id")
    if (seq == 1) != (prev is None):
        raise WitnessFormatError("witness entry prev is null iff seq == 1")
    if not _is_hash(publisher):
        raise WitnessFormatError("witness entry publisher must be a 64-char hex pubkey")
    if not isinstance(org, str) or not org:
        raise WitnessFormatError("witness entry org must be a non-empty string")
    # Key order is fixed by _ENTRY_FIELDS_V2 through canonical_json, which sorts
    # keys — so field insertion order here is irrelevant to the content address.
    return {
        "v": WITNESS_VERSION_2,
        "org": org,
        "seq": seq,
        "prev": prev,
        "t": t,
        "heads": {topic: canonical_heads[topic] for topic in sorted(canonical_heads)},
        "publisher": publisher,
    }


def validate_entry_v2(entry: object) -> dict:
    """Re-validate a received v2 entry, returning it in canonical form.

    Rejects a missing/extra field, a ``v`` that is not 2, an unknown topic, an
    unsorted or duplicated head list, ``t < 0``, and the ``prev``/``seq``
    disagreement — the same anti-malleability posture as :func:`validate_entry`,
    one level down into the grouped shape. A v1-shaped entry (carrying ``topic``,
    no ``v``) fails closed here.
    """
    if not isinstance(entry, dict):
        raise WitnessFormatError("witness entry must be a JSON object")
    if set(entry) != set(_ENTRY_FIELDS_V2):
        raise WitnessFormatError(
            f"v2 witness entry must carry exactly {list(_ENTRY_FIELDS_V2)}"
        )
    if entry["v"] != WITNESS_VERSION_2:
        raise WitnessFormatError("v2 witness entry must carry v == 2")
    if not isinstance(entry["heads"], dict):
        raise WitnessFormatError("v2 witness entry heads must be a topic-keyed object")
    rebuilt = build_entry_v2(
        entry["org"], entry["seq"], entry["heads"], entry["prev"],
        entry["t"], entry["publisher"],
    )
    if entry["heads"] != rebuilt["heads"]:
        raise WitnessFormatError(
            "v2 witness entry heads must be per-topic sorted and duplicate-free"
        )
    return rebuilt


def validate_any_entry(entry: object) -> dict:
    """Validate an entry of either version, dispatching on its shape.

    A ``v: 2`` entry goes to :func:`validate_entry_v2`; an entry with no ``v``
    field goes to the v1 :func:`validate_entry`. Any other ``v`` fails closed,
    so a future version cannot be silently accepted under an old validator.
    """
    if isinstance(entry, dict) and "v" in entry:
        if entry.get("v") != WITNESS_VERSION_2:
            raise WitnessFormatError(f"unknown witness entry version {entry.get('v')!r}")
        return validate_entry_v2(entry)
    return validate_entry(entry)


def sign_attestation_v2(witness_key: KeyPair, entry: dict) -> dict:
    """Mint a served v2 attestation. The signature is domain-separated to v2 via
    :func:`attestation_signing_input`, which dispatches on ``entry["v"]``."""
    if entry.get("v") != WITNESS_VERSION_2:
        raise WitnessFormatError("sign_attestation_v2 requires a v2 entry")
    return sign_attestation(witness_key, entry)


def verify_attestation_v2(attestation: object, witness_pub: str) -> dict:
    """Verify a served v2 attestation against a pinned witness key.

    Fails closed on a v1-shaped entry (no ``v: 2``): a client expecting a v2
    attestation must not accept a v1 one smuggled in its place.
    """
    if not isinstance(attestation, dict):
        raise WitnessFormatError("attestation must be a JSON object")
    entry = attestation.get("entry")
    if not isinstance(entry, dict) or entry.get("v") != WITNESS_VERSION_2:
        raise WitnessFormatError("expected a v2 attestation entry")
    return _verify_attestation_with(attestation, witness_pub, validate_entry_v2)
