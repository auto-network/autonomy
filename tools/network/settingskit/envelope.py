"""The signed-settings envelope: sign the addressed record, not the payload.

Every Setting row in an organization database with a founded ledger carries a
signature over the *addressed record* — the payload plus every field that
addresses it — so the same bytes can never verify at another address or in
another organization. Design of record: graph://21a0da9e-1c2; attested time:
graph://a8ae94f4-059.

The record is::

    {org, set_id, key, schema_revision, publication_state, deprecated,
     successor_id, payload, signed_at, signing_key, witness}

- ``org`` is the organization's GENESIS ID (64-hex), never its slug: slugs are
  renameable in place and the signed bytes must outlive a rename.
- ``signed_at`` is unix milliseconds. The witness attestation's ``t`` is unix
  seconds; the boundary converts when it bounds one against the other.
  Milliseconds because the per-signer freshness floor refuses a write that is
  not strictly newer than the signer's own slot, and second granularity would
  refuse two legitimate writes in one second.
- ``witness`` is the witness attestation the signer held when signing, carried
  as the served object (opaque here — its internal shape belongs to the
  witness protocol and is checked at the boundary, not by the encoding).
  ``None`` states that the organization has never published and has no
  attestation to cite. The field is always present: "no witness" is an
  explicit ``null`` in the signed bytes, never a missing key, so both
  builders produce identical bytes for it.
- ``signing_key`` is required for BOTH key strategies. After a rekey the
  signing key is not the row key, so a row that cannot name its signer cannot
  be written at all.

The signature is Ed25519 over ``SETTINGS_ENVELOPE_DOMAIN || canonical_json(record)``.
Both languages share one canonicalization: :mod:`tools.network.idkit.canonical`
here, ``canonicalJson`` in ``ceremony/primitives.js`` in the browser. Do not
add a second one.
"""

from __future__ import annotations

import json

from tools.network.idkit import KeyPair, canonical_json, verify_signature
from tools.network.idkit.errors import MalformedError
from tools.network.idkit.keys import PUBLIC_KEY_HEX_LEN, _decode_hex

SETTINGS_ENVELOPE_DOMAIN = b"autonomy.network.settings.envelope.v1\n"

PUBLICATION_STATES = ("raw", "curated", "published", "canonical")

ENVELOPE_FIELDS = (
    "org",
    "set_id",
    "key",
    "schema_revision",
    "publication_state",
    "deprecated",
    "successor_id",
    "payload",
    "signed_at",
    "signing_key",
    "witness",
)

_GENESIS_ID_HEX_LEN = 64


class EnvelopeFormatError(MalformedError):
    """A settings envelope record is structurally malformed."""


def _require_str(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise EnvelopeFormatError(f"{what} must be a non-empty string")
    return value


def _require_hex(value: object, length: int, what: str) -> str:
    _require_str(value, what)
    try:
        _decode_hex(value, length, what)
    except MalformedError as exc:
        raise EnvelopeFormatError(str(exc)) from None
    return value  # type: ignore[return-value]


def build_record(
    *,
    org: str,
    set_id: str,
    key: str,
    schema_revision: int,
    publication_state: str,
    deprecated: bool,
    successor_id: str | None,
    payload: dict,
    signed_at: int,
    signing_key: str,
    witness: dict | None,
) -> dict:
    """Assemble the addressed record (validated, canonical field set).

    Raises :class:`EnvelopeFormatError` on any structural defect, including a
    missing or malformed ``signing_key`` — an envelope that cannot name its
    signer cannot exist, under either key strategy.
    """
    record = {
        "org": _require_hex(org, _GENESIS_ID_HEX_LEN, "org genesis id"),
        "set_id": _require_str(set_id, "set_id"),
        "key": _require_str(key, "key"),
        "schema_revision": schema_revision,
        "publication_state": publication_state,
        "deprecated": deprecated,
        "successor_id": successor_id,
        "payload": payload,
        "signed_at": signed_at,
        "signing_key": _require_hex(signing_key, PUBLIC_KEY_HEX_LEN, "signing_key"),
        "witness": witness,
    }
    if isinstance(schema_revision, bool) or not isinstance(schema_revision, int) \
            or schema_revision < 1:
        raise EnvelopeFormatError("schema_revision must be a positive integer")
    if publication_state not in PUBLICATION_STATES:
        raise EnvelopeFormatError(
            f"publication_state must be one of {list(PUBLICATION_STATES)}"
        )
    if not isinstance(deprecated, bool):
        raise EnvelopeFormatError("deprecated must be a boolean")
    if successor_id is not None:
        _require_str(successor_id, "successor_id")
    if not isinstance(payload, dict):
        raise EnvelopeFormatError("payload must be a JSON object")
    if isinstance(signed_at, bool) or not isinstance(signed_at, int) or signed_at < 0:
        raise EnvelopeFormatError(
            "signed_at must be a non-negative integer (unix milliseconds)"
        )
    if witness is not None and (not isinstance(witness, dict) or not witness):
        raise EnvelopeFormatError(
            "witness must be the served attestation object, or None for an "
            "organization that has never published"
        )
    # The canonical grammar is the last gate: floats, non-string keys and
    # foreign types anywhere in payload or witness are refused here.
    try:
        canonical_json(record)
    except MalformedError as exc:
        raise EnvelopeFormatError(str(exc)) from None
    return record


def validate_record(record: object) -> dict:
    """Re-validate a received record dict, refusing unknown or missing fields."""
    if not isinstance(record, dict):
        raise EnvelopeFormatError("envelope record must be a JSON object")
    if set(record) != set(ENVELOPE_FIELDS):
        missing = sorted(set(ENVELOPE_FIELDS) - set(record))
        unknown = sorted(set(record) - set(ENVELOPE_FIELDS))
        raise EnvelopeFormatError(
            "envelope record must carry exactly the addressed-record fields"
            + (f"; missing {missing}" if missing else "")
            + (f"; unknown {unknown}" if unknown else "")
        )
    return build_record(**{field: record[field] for field in ENVELOPE_FIELDS})


def record_bytes(record: dict) -> bytes:
    """The canonical bytes of the validated record."""
    return canonical_json(validate_record(record))


def signing_input(record: dict) -> bytes:
    """The exact bytes the envelope signature covers (domain-separated)."""
    return SETTINGS_ENVELOPE_DOMAIN + record_bytes(record)


def sign_record(keypair: KeyPair, record: dict) -> str:
    """Sign the record, requiring the keypair to BE the named signing key.

    The record names its signer; a signature by any other key would fail
    verification, so refusing the mismatch here turns a silent defect into a
    named one.
    """
    validated = validate_record(record)
    if keypair.public_hex != validated["signing_key"]:
        raise EnvelopeFormatError(
            "record.signing_key does not match the signing keypair"
        )
    return keypair.sign_hex(signing_input(validated))


def verify_record(record: dict, sig_hex: str) -> dict:
    """Verify *sig_hex* over the record against its own ``signing_key``.

    Returns the validated record on success. Raises
    :class:`~tools.network.idkit.errors.SignatureError` on mismatch and
    :class:`EnvelopeFormatError` on a malformed record. This is the
    cryptographic step only — resolving the key to a persona, membership,
    scope, revocation, currency and the witness bound are boundary checks
    (design of record, "Verification, at the boundaries") and are not
    performed here.
    """
    validated = validate_record(record)
    verify_signature(
        validated["signing_key"], sig_hex, SETTINGS_ENVELOPE_DOMAIN + canonical_json(validated)
    )
    return validated


def record_from_row(row, org: str) -> dict:
    """Rebuild the signed record from a stored settings row.

    *row* is any mapping with the settings columns (``sqlite3.Row`` works);
    *org* is the genesis id of the organization whose database holds the row —
    the store supplies it, the row does not repeat it. ``payload`` and
    ``witness`` may be stored JSON text or already-parsed objects;
    ``deprecated`` may be the stored 0/1. The result is the exact record the
    signature covers, so a stored column edited after signing —
    ``publication_state`` and ``deprecated`` included — makes verification
    fail.
    """
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    witness = row["witness"]
    if isinstance(witness, str):
        witness = json.loads(witness)
    return build_record(
        org=org,
        set_id=row["set_id"],
        key=row["key"],
        schema_revision=row["schema_revision"],
        publication_state=row["publication_state"],
        deprecated=bool(row["deprecated"]),
        successor_id=row["successor_id"],
        payload=payload,
        signed_at=row["signed_at"],
        signing_key=row["signing_key"],
        witness=witness,
    )
