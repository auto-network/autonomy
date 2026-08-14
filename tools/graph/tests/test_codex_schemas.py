"""Schema validation tests for ``dashboard.codex.credentials#1``
(bead auto-kzws9). Parity with ``test_claude_schemas.py``.
"""

from __future__ import annotations

import pytest

from tools.graph import schemas
from tools.graph.schemas.codex_credentials import (
    CODEX_CREDENTIALS_REVISION,
    CODEX_CREDENTIALS_SET_ID,
    CodexCredentialsV1,
)


def _valid_codex_payload() -> dict:
    return {
        "email": "dev@example.com",
        "auth_mode": "chatgpt",
        "access_token": "ct-1234",
        "refresh_token": "cr-1234",
        "id_token": "eyJhbGciOiJub25lIn0.eyJleHAiOjE5MDB9.sig",
        "expires_at_ms": 1_900_000_000_000,
    }


def test_codex_credentials_schema_registered():
    schema = schemas.get_schema(CODEX_CREDENTIALS_SET_ID, CODEX_CREDENTIALS_REVISION)
    assert schema is CodexCredentialsV1


def test_codex_credentials_is_keyed_per_entity():
    """Credentials are caller-keyed by account_id; no cache TTL."""
    assert CodexCredentialsV1._access_pattern == "keyed_per_entity"
    assert getattr(CodexCredentialsV1, "_cache_ttl_seconds", None) is None


def test_codex_full_valid_payload_passes():
    schemas.validate_payload(
        CODEX_CREDENTIALS_SET_ID,
        CODEX_CREDENTIALS_REVISION,
        _valid_codex_payload(),
    )


@pytest.mark.parametrize(
    "field",
    ["auth_mode", "access_token", "refresh_token", "id_token"],
)
def test_codex_required_string_fields(field):
    payload = _valid_codex_payload()
    payload[field] = ""
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CODEX_CREDENTIALS_SET_ID, CODEX_CREDENTIALS_REVISION, payload,
        )


def test_codex_expires_at_ms_must_be_int():
    payload = _valid_codex_payload()
    payload["expires_at_ms"] = "1900000000000"
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CODEX_CREDENTIALS_SET_ID, CODEX_CREDENTIALS_REVISION, payload,
        )


def test_codex_expires_at_ms_rejects_bool():
    payload = _valid_codex_payload()
    payload["expires_at_ms"] = True
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CODEX_CREDENTIALS_SET_ID, CODEX_CREDENTIALS_REVISION, payload,
        )


def test_codex_email_optional_and_nullable():
    payload = _valid_codex_payload()
    payload["email"] = None
    schemas.validate_payload(
        CODEX_CREDENTIALS_SET_ID, CODEX_CREDENTIALS_REVISION, payload,
    )
    del payload["email"]
    schemas.validate_payload(
        CODEX_CREDENTIALS_SET_ID, CODEX_CREDENTIALS_REVISION, payload,
    )
    payload["email"] = 12345  # not a string
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CODEX_CREDENTIALS_SET_ID, CODEX_CREDENTIALS_REVISION, payload,
        )


def test_codex_rejects_unknown_field():
    payload = _valid_codex_payload()
    payload["OPENAI_API_KEY"] = "sk-proj-would-leak-here"
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CODEX_CREDENTIALS_SET_ID, CODEX_CREDENTIALS_REVISION, payload,
        )


def test_codex_optional_refresh_fields_are_strings_or_null():
    payload = _valid_codex_payload()
    payload["last_refresh_at"] = "2026-08-14T00:00:00Z"
    payload["last_refresh_error"] = None
    schemas.validate_payload(
        CODEX_CREDENTIALS_SET_ID, CODEX_CREDENTIALS_REVISION, payload,
    )
    payload["last_refresh_at"] = 12345  # not a string
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CODEX_CREDENTIALS_SET_ID, CODEX_CREDENTIALS_REVISION, payload,
        )
