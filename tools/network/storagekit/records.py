"""Canonical signed-record helpers — defined once, reused by every record.

Storage key-control records follow the idkit record conventions
(``certs.py``, ``revocation.py``): the signature covers a domain prefix
followed by the canonical JSON of the payload without its signature
field; the wire form is the canonical JSON of the full record including
the signature; the record identifier is the SHA-256 of those signed
bytes (so the identifier commits to the signature); and parsing is
anti-malleable — everything a signature covers has exactly one accepted
byte form.
"""

from __future__ import annotations

import hashlib
import json

from tools.network.idkit import canonical_json
from tools.network.idkit.errors import MalformedError

from .errors import MalformedRecordError


def signing_input(domain_prefix: bytes, payload: dict) -> bytes:
    """Domain-separated signature input: prefix || canonical_json(payload)."""
    if not isinstance(domain_prefix, bytes) or not domain_prefix:
        raise MalformedRecordError("domain_prefix must be non-empty bytes")
    if not isinstance(payload, dict):
        raise MalformedRecordError("payload must be a dict")
    try:
        return domain_prefix + canonical_json(payload)
    except MalformedError as exc:
        raise MalformedRecordError(str(exc)) from exc


def record_id(canonical_bytes: bytes) -> str:
    """SHA-256 hex of a record's canonical wire bytes (its identity)."""
    if not isinstance(canonical_bytes, (bytes, bytearray)):
        raise MalformedRecordError("record bytes must be bytes")
    return hashlib.sha256(bytes(canonical_bytes)).hexdigest()


def parse_canonical(field_names, data) -> dict:
    """Strict-parse wire *data* into a dict with exactly *field_names*.

    Rejects unknown fields, missing fields, duplicate keys, and any wire
    whose re-serialization is not byte-identical to the input — reordered
    keys, whitespace, non-shortest escapes: one accepted byte form only.
    """
    expected = frozenset(field_names)
    if not isinstance(data, (bytes, bytearray)):
        raise MalformedRecordError("record wire must be bytes")
    data = bytes(data)

    def _no_dup_pairs(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise MalformedRecordError(f"record has duplicate key {k!r}")
            obj[k] = v
        return obj

    try:
        parsed = json.loads(data, object_pairs_hook=_no_dup_pairs)
    except MalformedRecordError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise MalformedRecordError(f"record wire does not parse as JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MalformedRecordError("record wire must be a JSON object")
    if set(parsed) != expected:
        missing = sorted(expected - set(parsed))
        unknown = sorted(set(parsed) - expected)
        raise MalformedRecordError(
            f"record fields do not match: missing {missing}, unknown {unknown}"
        )
    try:
        reserialized = canonical_json(parsed)
    except MalformedError as exc:
        raise MalformedRecordError(str(exc)) from exc
    if reserialized != data:
        raise MalformedRecordError("record wire is not in canonical byte form")
    return parsed
