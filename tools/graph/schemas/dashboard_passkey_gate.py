"""The dashboard's passkey gate record (graph://c9d72ea4-feb §10, option O2d).

One row per operator, ``personal``-homed: the dashboard's relay route is
published under the operator's persona label, so the same passkey must open
it from whichever fleet machine serves the route. It holds the enrolled gate
passkeys (public keys only) and the enrollment state. The one-time enrollment
token is never stored here: its hash is, and its plaintext lives in the
machine vault while enrollment is open. The gate helper
(tools/network/passkey_gate.py) reads a projection of this row that the
dashboard materializes into the helper's runtime directory.
"""

from __future__ import annotations

import re
from typing import Any

from .namespace_reservation import _is_rfc3339_millis
from .registry import SchemaValidationError, SettingSchema, field, home, publication_band, singleton

PASSKEY_GATE_SET_ID = "autonomy.dashboard.passkey-gate"

SYNOPSIS = {
    "summary": "The dashboard's passkey gate: enrolled gate passkeys (public keys and sign counts) and the one-time enrollment state (a token hash and expiry, never the token).",
    "nouns": ["passkey gate", "WebAuthn", "forward auth", "enrollment token", "remote access"],
    "related_set_ids": ["autonomy.dashboard.remote-access#1"],
}
PASSKEY_GATE_REVISION = 1
PASSKEY_GATE_KEY = "default"

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRANSPORTS = ("ble", "cable", "hybrid", "internal", "nfc", "smart-card", "usb")


@home("personal")
@publication_band(max="raw")
@singleton(key=PASSKEY_GATE_KEY)
class DashboardPasskeyGateV1(SettingSchema):
    """Enrolled gate passkeys and the enrollment state of the dashboard's relay route."""

    set_id = PASSKEY_GATE_SET_ID
    schema_revision = PASSKEY_GATE_REVISION

    credentials: list = field(
        required=True,
        description="Enrolled gate passkeys: [{credential_id, public_key (COSE, base64url), "
                    "sign_count, transports, rp_id, created_at}].",
    )
    enrollment: dict = field(
        required=False,
        description="While enrollment is open: {open: true, token_sha256, expires_at (unix seconds), "
                    "opened_by}. Absent or open=false otherwise.",
    )
    updated_at: str = field(required=True, description="UTC RFC 3339 with milliseconds.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        credentials = payload.get("credentials")
        if not isinstance(credentials, list):
            raise SchemaValidationError(f"{cls.__name__}: credentials must be a list")
        seen: set[str] = set()
        for row in credentials:
            if not isinstance(row, dict):
                raise SchemaValidationError(f"{cls.__name__}: each credential is an object")
            for name in ("credential_id", "public_key"):
                value = row.get(name)
                if not isinstance(value, str) or not _B64URL_RE.fullmatch(value):
                    raise SchemaValidationError(f"{cls.__name__}: credential {name} must be base64url")
            if row["credential_id"] in seen:
                raise SchemaValidationError(f"{cls.__name__}: duplicate credential_id")
            seen.add(row["credential_id"])
            if not isinstance(row.get("sign_count"), int) or row["sign_count"] < 0:
                raise SchemaValidationError(f"{cls.__name__}: sign_count must be a non-negative integer")
            transports = row.get("transports", [])
            if not isinstance(transports, list) or any(t not in TRANSPORTS for t in transports):
                raise SchemaValidationError(f"{cls.__name__}: transports must name WebAuthn transports")
            if not isinstance(row.get("rp_id"), str) or not row["rp_id"]:
                raise SchemaValidationError(f"{cls.__name__}: credential rp_id is required")
            if not _is_rfc3339_millis(row.get("created_at")):
                raise SchemaValidationError(f"{cls.__name__}: credential created_at must be UTC RFC 3339 with milliseconds")
        enrollment = payload.get("enrollment")
        if enrollment is not None:
            if not isinstance(enrollment, dict) or not isinstance(enrollment.get("open"), bool):
                raise SchemaValidationError(f"{cls.__name__}: enrollment carries a boolean open")
            if enrollment["open"]:
                if not isinstance(enrollment.get("token_sha256"), str) or not _SHA256_RE.fullmatch(enrollment["token_sha256"]):
                    raise SchemaValidationError(f"{cls.__name__}: an open enrollment carries token_sha256")
                if not isinstance(enrollment.get("expires_at"), (int, float)) or isinstance(enrollment["expires_at"], bool):
                    raise SchemaValidationError(f"{cls.__name__}: an open enrollment carries expires_at (unix seconds)")
                if not isinstance(enrollment.get("opened_by"), str) or not enrollment["opened_by"]:
                    raise SchemaValidationError(f"{cls.__name__}: an open enrollment names opened_by")
        if not _is_rfc3339_millis(payload.get("updated_at")):
            raise SchemaValidationError(f"{cls.__name__}: updated_at must be UTC RFC 3339 with milliseconds")
