"""Schema: ``dashboard.claude.setup_tokens#1``.

Per-account long-lived setup token (``sk-ant-oat01-…``) minted via
``POST /api/oauth/claude_cli/create_api_key`` from the console-scoped
OAuth flow. Keyed by the Anthropic organization UUID. Substrate
``expires_at = created_at + 1y`` — cache-gc sweeps rows past their
year, and the launcher's picker filters on it.

The console OAuth bundle itself is **not** persisted: only the minted
``raw_key`` survives, because the design treats setup tokens as
non-refreshable (re-mint when stale) and the console bundle has no
further use after the mint call.

Spec: graph://73c4e9ef-bbc.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .registry import (
    publication_band,
    keyed_per_entity,
    SchemaValidationError,
    SettingSchema,
    cache,
    field,
)


CLAUDE_SETUP_TOKENS_SET_ID = "dashboard.claude.setup_tokens"
CLAUDE_SETUP_TOKENS_REVISION = 1

# Year-long Anthropic-side raw_key lifetime; substrate cache-gc sweeps
# the row when ``expires_at`` elapses.
CLAUDE_SETUP_TOKEN_TTL = timedelta(days=365)


SYNOPSIS = {
    "summary": (
        "Per-account long-lived setup token (sk-ant-oat01-…) minted from the "
        "console OAuth flow. Keyed by Anthropic organization UUID; substrate "
        "expires_at = created_at + 1y. Launcher reads this set to pick an "
        "auth identity for a fresh container."
    ),
    "nouns": [
        "claude setup token", "raw_key", "sk-ant-oat01",
        "container auth", "launcher token",
    ],
    "related_set_ids": [
        "dashboard.claude.credentials#1",
    ],
}


@cache(ttl=CLAUDE_SETUP_TOKEN_TTL)
#: Never leaves the database that owns it: harness setup tokens. Publication state
#: is the only control over a cross-organization read, so the band is
#: what makes 'promote this' unable to become a disclosure.
@publication_band(max="raw")
@keyed_per_entity(key_strategy="account_uuid")
class ClaudeSetupTokenV1(SettingSchema):
    """Per-account long-lived setup token.

    Key: Anthropic organization UUID. Substrate ``expires_at`` is
    ``created_at + 1y``; the cache-gc sweep removes rows past their
    year, and the launcher's picker filters on it.

    The payload deliberately carries only ``raw_key``: substrate
    ``created_at`` records when minted and ``expires_at`` records when
    stale, so no other fields are needed.
    """

    set_id = CLAUDE_SETUP_TOKENS_SET_ID
    schema_revision = CLAUDE_SETUP_TOKENS_REVISION

    raw_key: str = field(
        required=True,
        description=(
            "Anthropic-minted setup token (``sk-ant-oat01-…``). Bearer "
            "credential — never logged or printed outside the substrate row."
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
        raw_key = payload.get("raw_key")
        if not isinstance(raw_key, str) or not raw_key:
            raise SchemaValidationError(
                f"{cls.__name__}: 'raw_key' must be a non-empty string"
            )
