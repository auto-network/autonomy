"""Schema validation tests for ``dashboard.claude.credentials#1`` and
``dashboard.claude.setup_tokens#1`` (graph://73c4e9ef-bbc, bead auto-tyrly).
"""

from __future__ import annotations

import pytest

from tools.graph import schemas
from tools.graph.schemas.claude_credentials import (
    CLAUDE_CREDENTIALS_REVISION,
    CLAUDE_CREDENTIALS_SET_ID,
    ClaudeCredentialsV1,
)
from tools.graph.schemas.claude_setup_tokens import (
    CLAUDE_SETUP_TOKEN_TTL,
    CLAUDE_SETUP_TOKENS_REVISION,
    CLAUDE_SETUP_TOKENS_SET_ID,
    ClaudeSetupTokenV1,
)


def _valid_credentials_payload() -> dict:
    return {
        "alias": "gmail-max",
        "organization_name": "Example Org",
        "account_email": "max@example.com",
        "access_token": "at-1234",
        "refresh_token": "rt-1234",
        "expires_at_ms": 1_900_000_000_000,
        "scopes": [
            "user:profile",
            "user:inference",
            "user:sessions:claude_code",
            "user:mcp_servers",
            "user:file_upload",
        ],
    }


def test_credentials_schema_registered():
    schema = schemas.get_schema(CLAUDE_CREDENTIALS_SET_ID, CLAUDE_CREDENTIALS_REVISION)
    assert schema is ClaudeCredentialsV1


def test_setup_tokens_schema_registered():
    schema = schemas.get_schema(CLAUDE_SETUP_TOKENS_SET_ID, CLAUDE_SETUP_TOKENS_REVISION)
    assert schema is ClaudeSetupTokenV1


def test_setup_tokens_uses_one_year_cache_ttl():
    """``@cache(ttl=365d)`` is the contract — substrate stamps expires_at."""
    seconds = getattr(ClaudeSetupTokenV1, "_cache_ttl_seconds", None)
    assert seconds is not None
    assert seconds == int(CLAUDE_SETUP_TOKEN_TTL.total_seconds())


def test_credentials_is_keyed_per_entity():
    """Credentials are caller-keyed by org UUID; no cache TTL."""
    assert ClaudeCredentialsV1._access_pattern == "keyed_per_entity"
    assert getattr(ClaudeCredentialsV1, "_cache_ttl_seconds", None) is None


def test_credentials_full_valid_payload_passes():
    schemas.validate_payload(
        CLAUDE_CREDENTIALS_SET_ID,
        CLAUDE_CREDENTIALS_REVISION,
        _valid_credentials_payload(),
    )


@pytest.mark.parametrize(
    "field",
    ["alias", "organization_name", "account_email", "access_token", "refresh_token"],
)
def test_credentials_required_string_fields(field):
    payload = _valid_credentials_payload()
    payload[field] = ""
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CLAUDE_CREDENTIALS_SET_ID, CLAUDE_CREDENTIALS_REVISION, payload,
        )


def test_credentials_expires_at_ms_must_be_int():
    payload = _valid_credentials_payload()
    payload["expires_at_ms"] = "1900000000000"
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CLAUDE_CREDENTIALS_SET_ID, CLAUDE_CREDENTIALS_REVISION, payload,
        )


def test_credentials_scopes_must_be_list_of_strings():
    payload = _valid_credentials_payload()
    payload["scopes"] = "user:profile user:inference"  # not a list
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CLAUDE_CREDENTIALS_SET_ID, CLAUDE_CREDENTIALS_REVISION, payload,
        )


def test_credentials_rejects_unknown_field():
    payload = _valid_credentials_payload()
    payload["raw_key"] = "sk-ant-oat01-XXX"
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CLAUDE_CREDENTIALS_SET_ID, CLAUDE_CREDENTIALS_REVISION, payload,
        )


def test_credentials_optional_refresh_fields_are_strings_or_null():
    payload = _valid_credentials_payload()
    payload["last_refresh_at"] = "2026-05-06T00:00:00Z"
    payload["last_refresh_error"] = None
    schemas.validate_payload(
        CLAUDE_CREDENTIALS_SET_ID, CLAUDE_CREDENTIALS_REVISION, payload,
    )
    payload["last_refresh_at"] = 12345  # not a string
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CLAUDE_CREDENTIALS_SET_ID, CLAUDE_CREDENTIALS_REVISION, payload,
        )


def test_setup_tokens_minimal_payload_passes():
    schemas.validate_payload(
        CLAUDE_SETUP_TOKENS_SET_ID,
        CLAUDE_SETUP_TOKENS_REVISION,
        {"raw_key": "sk-ant-oat01-XXXXX"},
    )


def test_setup_tokens_rejects_empty_raw_key():
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CLAUDE_SETUP_TOKENS_SET_ID,
            CLAUDE_SETUP_TOKENS_REVISION,
            {"raw_key": ""},
        )


def test_setup_tokens_rejects_unknown_field():
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate_payload(
            CLAUDE_SETUP_TOKENS_SET_ID,
            CLAUDE_SETUP_TOKENS_REVISION,
            {"raw_key": "sk-ant-oat01-X", "alias": "would-leak-here"},
        )
