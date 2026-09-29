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

The signature is Ed25519 over ``SETTINGS_ENVELOPE_DOMAIN || canonical_envelope_json(record)``:
idkit's canonical JSON rules plus floats in ECMAScript number form.
Both languages share one canonicalization: :mod:`tools.network.idkit.canonical`
here, ``canonicalJson`` in ``ceremony/primitives.js`` in the browser. Do not
add a second one.
"""

from __future__ import annotations

import json

import json
import math

from tools.network.idkit import KeyPair, verify_signature
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

#: JavaScript's Number.isSafeInteger bound. Both builders must encode every
#: envelope, so an integer only one of them can represent is not an envelope
#: value: Python's canonical_json is unbounded, the browser encoder throws
#: outside ±(2^53 − 1), and a record that admits the difference breaks D11.
#: Bounded HERE, on the envelope path, recursively — not in idkit's
#: canonical.py, whose blast radius (every signed record in the system) is
#: not this module's to take.
MAX_SAFE_INTEGER = 2**53 - 1


class EnvelopeFormatError(MalformedError):
    """A settings envelope record is structurally malformed."""


def ecmascript_number(value: float) -> str:
    """The ECMAScript ``Number::toString`` form of a finite float: the
    shortest round-trip digits (which Python's ``repr`` also produces),
    laid out exactly as JavaScript lays them out — ``0.1``, ``100``,
    ``1e+21``, ``1e-7``, ``0.000001`` — so the browser builder's
    ``String(number)`` and this encoder agree byte for byte. ``-0`` is
    ``0``, as in JavaScript. Non-finite values are refused."""
    if isinstance(value, bool) or not isinstance(value, float):
        raise EnvelopeFormatError("ecmascript_number takes a float")
    if not math.isfinite(value):
        raise EnvelopeFormatError("non-finite floats have no JSON encoding")
    if value == 0.0:
        return "0"
    if value.is_integer() and MAX_SAFE_INTEGER < abs(value) < 1e21:
        # Prints as plain digits (no exponent) that JavaScript cannot round
        # trip and that Python reads back as an INTEGER outside D11's
        # domain: the two sides would disagree, so both refuse it. From
        # 1e21 the form carries an exponent and reads back as a float.
        raise EnvelopeFormatError(
            f"integer-valued float {value!r} is outside the JavaScript-safe "
            f"range and has no shared encoding"
        )
    text = repr(value)
    sign = ""
    if text[0] == "-":
        sign, text = "-", text[1:]
    mantissa, _, exponent = text.partition("e")
    e = int(exponent) if exponent else 0
    integer_part, _, fraction = mantissa.partition(".")
    digits = (integer_part + fraction).lstrip("0")
    scale = e - len(fraction)
    stripped = digits.rstrip("0")
    scale += len(digits) - len(stripped)
    digits = stripped
    k = len(digits)
    n = k + scale                      # value = 0.d1…dk × 10^n
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        exp10 = n - 1
        head = digits[0] + ("." + digits[1:] if k > 1 else "")
        out = f"{head}e{'+' if exp10 >= 0 else '-'}{abs(exp10)}"
    return sign + out


def _canonical_value(value: object, where: str) -> str:
    """Canonical JSON text of one envelope value: the idkit rules (sorted
    keys, no whitespace, ASCII-only, no NaN) plus floats in ECMAScript
    form. Floats ARE allowed here, unlike idkit's canonical_json: a
    settings payload is application data (timings, ratios) and refusing
    them refused every such write (live 2026-09-29: the testing plugin's
    event log failed on every write for an hour after S2)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        try:
            return ecmascript_number(value)
        except EnvelopeFormatError as exc:
            raise EnvelopeFormatError(f"{where}: {exc}") from None
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise EnvelopeFormatError(f"{where}: object keys must be strings")
            parts.append(json.dumps(key, ensure_ascii=True) + ":" + _canonical_value(value[key], f"{where}.{key}"))
        return "{" + ",".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_value(v, f"{where}[{i}]") for i, v in enumerate(value)) + "]"
    raise EnvelopeFormatError(
        f"{where}: type {type(value).__name__} is not allowed in a settings envelope"
    )


def canonical_envelope_json(record: dict) -> bytes:
    """The envelope's canonical bytes (the JS builder's canonicalEnvelopeJson)."""
    return _canonical_value(record, "record").encode("ascii")


def _check_safe_integers(value: object, where: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise EnvelopeFormatError(
                f"{where}: integer {value} is outside the JavaScript-safe "
                f"range ±{MAX_SAFE_INTEGER} and has no browser encoding"
            )
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _check_safe_integers(v, f"{where}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check_safe_integers(v, f"{where}[{i}]")


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
    # The payload is whatever the row stores: an object for a plain set, a
    # JSON string for a vault-sealed set (the sealed blob), an array or a
    # scalar where a schema says so. The canonical grammar below is the
    # only shape rule.
    if isinstance(signed_at, bool) or not isinstance(signed_at, int) or signed_at < 0:
        raise EnvelopeFormatError(
            "signed_at must be a non-negative integer (unix milliseconds)"
        )
    if witness is not None and (not isinstance(witness, dict) or not witness):
        raise EnvelopeFormatError(
            "witness must be the served attestation object, or None for an "
            "organization that has never published"
        )
    # The canonical grammar is the last gate: non-finite floats, non-string
    # keys and foreign types anywhere in payload or witness are refused.
    canonical_envelope_json(record)
    # Then the shared-domain gate: every integer anywhere in the record must
    # be representable by BOTH builders (D11), so the JavaScript-safe bound
    # applies recursively — payload and witness included.
    _check_safe_integers(record, "record")
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
    return canonical_envelope_json(validate_record(record))


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
        validated["signing_key"], sig_hex, SETTINGS_ENVELOPE_DOMAIN + canonical_envelope_json(validated)
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
