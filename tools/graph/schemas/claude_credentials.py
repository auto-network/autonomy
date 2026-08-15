"""Schema: ``dashboard.claude.credentials#1``.

Per-account Claude OAuth bundle (consumer scope). Replaces the legacy
``~/.claude/.credentials.<id>.json`` disk files as the unit of credential
management. Keyed by the Anthropic organization UUID; the operator-friendly
``alias`` lives on the payload so the dashboard can render the friendly name
via a join on the org UUID.

The refresh poller (separate bead) rotates ``access_token`` /
``refresh_token`` every ~4h and stamps ``last_refresh_at`` /
``last_refresh_error``. Install (this bead) writes the initial bundle.

Spec: graph://73c4e9ef-bbc.
"""

from __future__ import annotations

from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)


CLAUDE_CREDENTIALS_SET_ID = "dashboard.claude.credentials"
CLAUDE_CREDENTIALS_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Per-account Claude OAuth bundle (consumer scope). Keyed by Anthropic "
        "organization UUID. Operator-friendly alias lives on the payload; the "
        "refresh poller rotates access_token/refresh_token every ~4h."
    ),
    "nouns": [
        "claude credentials", "oauth bundle", "refresh token",
        "claude install", "alias", "anthropic org",
    ],
    "related_set_ids": [
        "dashboard.claude.setup_tokens#1",
        "dashboard.harness.usage#1",
    ],
}


@keyed_per_entity(key_strategy="account_uuid")
class ClaudeCredentialsV1(SettingSchema):
    """Per-account Claude OAuth credentials.

    Key: Anthropic organization UUID (``organization.uuid`` from the
    OAuth response). Payload carries the rotating bundle plus the
    operator-friendly ``alias`` set at install time.
    """

    set_id = CLAUDE_CREDENTIALS_SET_ID
    schema_revision = CLAUDE_CREDENTIALS_REVISION

    alias: str = field(
        required=True,
        description=(
            "Operator-set free-form name (e.g. 'gmail-max', 'jeremy-auto'). "
            "Stored on the credentials row so the dashboard can render the "
            "friendly name without operators ever seeing the org UUID."
        ),
    )
    organization_name: str = field(
        required=True,
        description="Anthropic-side organization name from the OAuth response.",
    )
    account_email: str = field(
        required=True,
        description="Anthropic-side account email from the OAuth response.",
    )
    access_token: str = field(
        required=True,
        description=(
            "Current OAuth access token (consumer scope). Rotates on every "
            "refresh."
        ),
    )
    refresh_token: str = field(
        required=True,
        description=(
            "Current OAuth refresh token. Rotates on every refresh — "
            "Anthropic always returns a new one."
        ),
    )
    expires_at_ms: int = field(
        required=True,
        description=(
            "Access-token expiry in epoch milliseconds. Computed at write "
            "time as ``created_at + expires_in * 1000``; the refresh "
            "poller restamps on every rotation."
        ),
    )
    scopes: list = field(
        required=True,
        description=(
            "Space-split scope list returned by the OAuth response (consumer "
            "scope set: user:profile, user:inference, user:sessions:claude_code, "
            "user:mcp_servers, user:file_upload)."
        ),
        element={"type": "string"},
    )
    last_refresh_at: str | None = field(
        default=None,
        description=(
            "ISO-8601 timestamp of the most recent refresh by the poller. "
            "Absent until the refresh poller (separate bead) runs."
        ),
    )
    last_refresh_error: str | None = field(
        default=None,
        description=(
            "Most recent refresh-poller error string. Present only when the "
            "last refresh failed; cleared on the next successful refresh."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )
        for required_field in (
            "alias", "organization_name", "account_email",
            "access_token", "refresh_token",
        ):
            value = payload.get(required_field)
            if not isinstance(value, str) or not value:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field "
                    f"{required_field!r}"
                )
        expires = payload.get("expires_at_ms")
        if isinstance(expires, bool) or not isinstance(expires, int):
            raise SchemaValidationError(
                f"{cls.__name__}: 'expires_at_ms' must be an integer "
                "(epoch milliseconds)"
            )
        scopes = payload.get("scopes")
        if not isinstance(scopes, list) or not all(
            isinstance(s, str) and s for s in scopes
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'scopes' must be a list of non-empty strings"
            )
        for opt in ("last_refresh_at", "last_refresh_error"):
            v = payload.get(opt)
            if v is not None and not isinstance(v, str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {opt!r} must be a string or null"
                )
