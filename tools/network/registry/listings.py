"""Listing registry wire formats — signed listing claims + attestations (L1).

Design ``graph://29ff28a8-b39``, bead ``auto-8nk01``. The registry is a
directory of shareable things: an org publishes a signed **listing** — a
small card naming the publisher, a content hash of the thing, a name,
version, description, and icon — and others discover it. The registry
stores the CARD only (KB), never the bundle: bundles are content-addressed
and fetched from any org that holds bytes matching the hash.

This module owns the **wire format** and **signing/verification**
primitives shared by the server that accepts a claim and the client that
mints or re-verifies one — the same split as :mod:`registry.witness`.

Wire shapes
-----------
A *listing claim* is a payload plus the publisher-side signature over it::

    payload = {"v", "publisher", "name", "version", "bundle_hash",
               "description", "icon", "provider_hints", "prev", "ts",
               "signer"}
    claim   = {"payload": {...}, "sig": <128 hex>, "cert": <wire str>?}
    sig     = Ed25519(signer_priv, LISTING_DOMAIN || canonical_json(payload))
    listing_id = sha256(canonical_json(payload))       # content address

``cert`` is the signer's delegation certificate (chain embedded, canonical
wire string) and is absent when the signer is the publisher org's root key
itself. Chain verification against the publisher's *bound* root is the
acceptor's job — it needs the binding and the org's live revocation set,
which are registry state, not wire format.

``prev`` makes a claim an *update*: it names the predecessor listing's
content address. An update is only an update if it chains to the same
publisher KEY-CONTINUITY (spec: account is never authority) — also
enforced by the acceptor, because continuity lives in the rebind trail.

An *attestation* is the SAME shape of signed claim, minted by any third
party vouching that a subject key controls a display name or domain::

    payload = {"v", "attestor", "subject", "claim_type", "claim_value",
               "evidence_type", "evidence", "ts", "ttl"}
    record  = {"payload": {...}, "sig": <128 hex>}
    sig     = Ed25519(attestor_priv, ATTESTATION_DOMAIN || canonical_json(payload))
    attestation_id = sha256(canonical_json(payload))

The registry verifies only the SIGNATURE of an attestation (so a record is
authentic to its attestor key) and stores it; the claim's content and
evidence are never trusted server-side — the CLIENT decides which
attestors it believes (impersonation is a rendering problem, not an
arbitration problem). Names are labels, not property: listings are keyed
``publisher/name``, so two orgs may both claim the same name and neither
is privileged.

Both formats are strictly anti-malleable: one accepted byte form
(canonical JSON), so a given claim has exactly one content address.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Optional

from tools.network.idkit import (
    DelegationCert,
    KeyPair,
    MalformedError,
    canonical_json,
    verify_signature,
)
from tools.network.idkit.keys import PUBLIC_KEY_HEX_LEN, _decode_hex

LISTING_DOMAIN = b"autonomy.network.registry.listing.v1\n"
ATTESTATION_DOMAIN = b"autonomy.network.registry.attestation.v1\n"
LISTING_VERSION = 1
ATTESTATION_VERSION = 1

#: Listing names are KEYS (path segments, `publisher/name`), not display
#: strings — same grammar as broker topics. Display naming is what
#: attestations are for.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")
_HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")
_MAX_TS = 2**63 - 1

# The card must stay KB-sized (the registry is a directory, not a CDN).
MAX_VERSION_CHARS = 64
MAX_DESCRIPTION_CHARS = 4_096
MAX_ICON_CHARS = 65_536  # small data-URI / base64 icon
MAX_PROVIDER_HINTS = 16
MAX_PROVIDER_HINT_CHARS = 256

ATTESTATION_CLAIM_TYPES = frozenset({"display_name", "domain"})
MAX_CLAIM_VALUE_CHARS = 256
MAX_EVIDENCE_TYPE_CHARS = 64
MAX_EVIDENCE_CHARS = 2_048
MAX_ATTESTATION_TTL = 365 * 86_400

_LISTING_FIELDS = (
    "v", "publisher", "name", "version", "bundle_hash", "description",
    "icon", "provider_hints", "prev", "ts", "signer",
)
_ATTESTATION_FIELDS = (
    "v", "attestor", "subject", "claim_type", "claim_value",
    "evidence_type", "evidence", "ts", "ttl",
)


class ListingFormatError(MalformedError):
    """A listing claim is structurally malformed."""


class AttestationFormatError(MalformedError):
    """An attestation record is structurally malformed."""


def _require_str(value: object, what: str, max_len: int, *,
                 err, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > max_len or (not allow_empty and not value):
        kind = "string" if allow_empty else "non-empty string"
        raise err(f"{what} must be a {kind} of at most {max_len} chars")
    return value


def _require_ts_field(value: object, what: str, err) -> int:
    # bool is an int subclass; reject it explicitly.
    if type(value) is not int or value < 0 or value > _MAX_TS:
        raise err(f"{what} must be an integer unix timestamp in [0, 2**63)")
    return value


def _require_pub_field(value: object, what: str, err) -> str:
    try:
        _decode_hex(value, PUBLIC_KEY_HEX_LEN, what)
    except MalformedError as exc:
        raise err(str(exc)) from exc
    return value  # type: ignore[return-value]


# -- listing claims -----------------------------------------------------------


def build_listing_payload(
    *,
    publisher: str,
    name: str,
    version: str,
    bundle_hash: str,
    description: str,
    icon: str,
    provider_hints: list,
    prev: Optional[str],
    ts: int,
    signer: str,
) -> dict:
    """Assemble a listing payload dict (validated, canonical field set)."""
    err = ListingFormatError
    if not isinstance(publisher, str):
        raise err("listing publisher must be an org UUID string")
    try:
        uuid.UUID(publisher)
    except (ValueError, TypeError):
        raise err("listing publisher is not a valid UUID")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise err("listing name must be 1-64 chars of [a-z0-9._-] starting alphanumeric")
    _require_str(version, "listing version", MAX_VERSION_CHARS, err=err)
    if not isinstance(bundle_hash, str) or not _HASH_RE.match(bundle_hash):
        raise err("listing bundle_hash must be a 64-char lowercase hex content hash")
    _require_str(description, "listing description", MAX_DESCRIPTION_CHARS,
                 err=err, allow_empty=True)
    _require_str(icon, "listing icon", MAX_ICON_CHARS, err=err, allow_empty=True)
    if not isinstance(provider_hints, list) or len(provider_hints) > MAX_PROVIDER_HINTS:
        raise err(f"listing provider_hints must be a list of at most {MAX_PROVIDER_HINTS} entries")
    for hint in provider_hints:
        _require_str(hint, "listing provider_hints entry", MAX_PROVIDER_HINT_CHARS, err=err)
    if prev is not None and (not isinstance(prev, str) or not _HASH_RE.match(prev)):
        raise err("listing prev must be null or a 64-char hex listing id")
    _require_ts_field(ts, "listing ts", err)
    _require_pub_field(signer, "listing signer", err)
    return {
        "v": LISTING_VERSION,
        "publisher": publisher,
        "name": name,
        "version": version,
        "bundle_hash": bundle_hash,
        "description": description,
        "icon": icon,
        "provider_hints": list(provider_hints),
        "prev": prev,
        "ts": ts,
        "signer": signer,
    }


def validate_listing_payload(obj: object) -> dict:
    """Re-validate a received payload, rejecting unknown/missing fields."""
    if not isinstance(obj, dict):
        raise ListingFormatError("listing payload must be a JSON object")
    if set(obj) != set(_LISTING_FIELDS):
        raise ListingFormatError(f"listing payload must carry exactly {list(_LISTING_FIELDS)}")
    if obj["v"] != LISTING_VERSION:
        raise ListingFormatError(f"unsupported listing version: {obj['v']!r}")
    return build_listing_payload(
        publisher=obj["publisher"], name=obj["name"], version=obj["version"],
        bundle_hash=obj["bundle_hash"], description=obj["description"],
        icon=obj["icon"], provider_hints=obj["provider_hints"],
        prev=obj["prev"], ts=obj["ts"], signer=obj["signer"],
    )


def listing_id(payload: dict) -> str:
    """Content address of a listing: sha256 of its canonical payload JSON."""
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def listing_signing_input(payload: dict) -> bytes:
    """The exact bytes a listing signature covers (domain-separated)."""
    return LISTING_DOMAIN + canonical_json(payload)


def sign_listing(key: KeyPair, payload: dict,
                 cert: Optional[DelegationCert] = None) -> str:
    """Mint a listing claim wire string (client-side counterpart).

    Pass *cert* when *key* is a delegated key (the cert must delegate to
    it); omit it when the publisher root signs directly.
    """
    payload = validate_listing_payload(payload)
    if payload["signer"] != key.public_hex:
        raise ValueError("payload signer does not match the signing key")
    if cert is not None and cert.child_pub != key.public_hex:
        raise ValueError("cert does not delegate to the signing key")
    claim = {
        "payload": payload,
        "sig": key.sign_hex(listing_signing_input(payload)),
    }
    if cert is not None:
        claim["cert"] = cert.to_json().decode("ascii")
    return canonical_json(claim).decode("ascii")


def parse_listing_claim(raw) -> dict:
    """Parse canonical claim wire and verify its signature.

    Strictly anti-malleable: any byte form other than the one
    :func:`sign_listing` produces is rejected, so a claim has exactly one
    content address. Verifies the signature against the payload's own
    ``signer``; whether that signer chains to the publisher's bound root
    is the acceptor's job (it needs registry state).

    Raises :class:`ListingFormatError` on malformed shape and an idkit
    ``SignatureError`` when the signature does not verify.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ListingFormatError("listing claim bytes are not valid UTF-8") from exc
    if not isinstance(raw, str):
        raise ListingFormatError("listing claim must be a canonical wire JSON string")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ListingFormatError("listing claim is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ListingFormatError("listing claim must be a JSON object")
    unknown = set(data) - {"payload", "sig", "cert"}
    if unknown:
        raise ListingFormatError(f"listing claim carries unknown fields: {sorted(unknown)}")
    missing = {"payload", "sig"} - set(data)
    if missing:
        raise ListingFormatError(f"listing claim is missing fields: {sorted(missing)}")
    payload = validate_listing_payload(data["payload"])
    if "cert" in data and not isinstance(data["cert"], str):
        raise ListingFormatError("listing claim cert must be a canonical wire JSON string")
    if canonical_json(data) != raw.encode("utf-8"):
        raise ListingFormatError("listing claim is not in canonical wire form")
    verify_signature(payload["signer"], data["sig"], listing_signing_input(payload))
    return {"payload": payload, "cert": data.get("cert"), "sig": data["sig"]}


# -- attestations -------------------------------------------------------------


def build_attestation_payload(
    *,
    attestor: str,
    subject: str,
    claim_type: str,
    claim_value: str,
    evidence_type: str,
    evidence: str,
    ts: int,
    ttl: int,
) -> dict:
    """Assemble an attestation payload dict (validated, canonical field set)."""
    err = AttestationFormatError
    _require_pub_field(attestor, "attestation attestor", err)
    _require_pub_field(subject, "attestation subject", err)
    if claim_type not in ATTESTATION_CLAIM_TYPES:
        raise err(f"attestation claim_type must be one of {sorted(ATTESTATION_CLAIM_TYPES)}")
    _require_str(claim_value, "attestation claim_value", MAX_CLAIM_VALUE_CHARS, err=err)
    _require_str(evidence_type, "attestation evidence_type", MAX_EVIDENCE_TYPE_CHARS, err=err)
    _require_str(evidence, "attestation evidence", MAX_EVIDENCE_CHARS,
                 err=err, allow_empty=True)
    _require_ts_field(ts, "attestation ts", err)
    if type(ttl) is not int or ttl < 1 or ttl > MAX_ATTESTATION_TTL:
        raise err(f"attestation ttl must be an integer in [1, {MAX_ATTESTATION_TTL}] seconds")
    return {
        "v": ATTESTATION_VERSION,
        "attestor": attestor,
        "subject": subject,
        "claim_type": claim_type,
        "claim_value": claim_value,
        "evidence_type": evidence_type,
        "evidence": evidence,
        "ts": ts,
        "ttl": ttl,
    }


def validate_attestation_payload(obj: object) -> dict:
    """Re-validate a received payload, rejecting unknown/missing fields."""
    if not isinstance(obj, dict):
        raise AttestationFormatError("attestation payload must be a JSON object")
    if set(obj) != set(_ATTESTATION_FIELDS):
        raise AttestationFormatError(
            f"attestation payload must carry exactly {list(_ATTESTATION_FIELDS)}"
        )
    if obj["v"] != ATTESTATION_VERSION:
        raise AttestationFormatError(f"unsupported attestation version: {obj['v']!r}")
    return build_attestation_payload(
        attestor=obj["attestor"], subject=obj["subject"],
        claim_type=obj["claim_type"], claim_value=obj["claim_value"],
        evidence_type=obj["evidence_type"], evidence=obj["evidence"],
        ts=obj["ts"], ttl=obj["ttl"],
    )


def attestation_id(payload: dict) -> str:
    """Content address of an attestation: sha256 of its canonical JSON."""
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def attestation_signing_input(payload: dict) -> bytes:
    """The exact bytes an attestation signature covers (domain-separated)."""
    return ATTESTATION_DOMAIN + canonical_json(payload)


def sign_attestation_record(key: KeyPair, payload: dict) -> str:
    """Mint an attestation record wire string, signed by the attestor key."""
    payload = validate_attestation_payload(payload)
    if payload["attestor"] != key.public_hex:
        raise ValueError("payload attestor does not match the signing key")
    record = {
        "payload": payload,
        "sig": key.sign_hex(attestation_signing_input(payload)),
    }
    return canonical_json(record).decode("ascii")


def parse_attestation_record(raw) -> dict:
    """Parse canonical record wire and verify it against its attestor key.

    Signature verification is the ONLY server-side trust step for an
    attestation: it makes the record authentic to the attestor key, so
    nobody can plant claims under a key they don't hold. Everything else
    — whether the evidence is real, whether the attestor is believed —
    is the client's call.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AttestationFormatError("attestation bytes are not valid UTF-8") from exc
    if not isinstance(raw, str):
        raise AttestationFormatError("attestation must be a canonical wire JSON string")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AttestationFormatError("attestation is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != {"payload", "sig"}:
        raise AttestationFormatError("attestation must carry exactly {payload, sig}")
    payload = validate_attestation_payload(data["payload"])
    if canonical_json(data) != raw.encode("utf-8"):
        raise AttestationFormatError("attestation is not in canonical wire form")
    verify_signature(payload["attestor"], data["sig"], attestation_signing_input(payload))
    return {"payload": payload, "sig": data["sig"]}
