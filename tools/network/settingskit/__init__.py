"""settingskit — the signed-settings envelope and its consumers.

The authenticity layer for Setting rows in organization databases: what is
signed, in which encoding, and how a stored row is rebuilt for verification.
Design of record: graph://21a0da9e-1c2 (signed settings).
"""

from .envelope import (
    ENVELOPE_FIELDS,
    EnvelopeFormatError,
    PUBLICATION_STATES,
    SETTINGS_ENVELOPE_DOMAIN,
    build_record,
    record_bytes,
    record_from_row,
    sign_record,
    signing_input,
    validate_record,
    verify_record,
)

__all__ = [
    "ENVELOPE_FIELDS",
    "EnvelopeFormatError",
    "PUBLICATION_STATES",
    "SETTINGS_ENVELOPE_DOMAIN",
    "build_record",
    "record_bytes",
    "record_from_row",
    "sign_record",
    "signing_input",
    "validate_record",
    "verify_record",
]
