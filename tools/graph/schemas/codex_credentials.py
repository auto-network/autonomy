"""Schema: ``dashboard.codex.credentials#1``.

Per-account Codex (ChatGPT-backed OAuth) credential bundle. The substrate
analog of the host-mounted ``~/.codex/auth.json`` — the unit of Codex
credential management, keyed by the ChatGPT ``account_id`` exactly as
``dashboard.claude.credentials`` is keyed by the Anthropic org UUID.

This is STEP 1 of the Codex credential end-state (bead auto-kzws9). It
establishes the substrate surface and its population only. The session
launcher and the host mount are deliberately UNTOUCHED by this bead — the
cutover (launcher reads substrate instead of the mount), the refresh
poller, and mount retirement are the successor's job. Until that lands,
this Setting is populated but not yet consumed; the launcher keeps reading
the read-only host mount.

Storage posture is like-for-like with Claude: personal org, host-local
secrets in ``personal.db``, plaintext tokens on the row. Both surfaces
upgrade together when vault C5 lands — this bead does not fork that
posture.

Fields mirror the Claude bundle's shape but carry Codex's token triple.
``account_id`` is the row key (not a payload field), matching the Claude
surface where the org UUID is the key rather than a stored field.

Spec: bead auto-kzws9. Parity reference: ``dashboard.claude.credentials#1``
(``graph://73c4e9ef-bbc``).
"""

from __future__ import annotations

from typing import Any

from .registry import (
    home,
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
    publication_band,
)


CODEX_CREDENTIALS_SET_ID = "dashboard.codex.credentials"
CODEX_CREDENTIALS_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Per-account Codex (ChatGPT-backed OAuth) credential bundle. Keyed by "
        "ChatGPT account_id. Substrate analog of the host-mounted "
        "~/.codex/auth.json; parity with dashboard.claude.credentials. "
        "Populated by `graph credentials import`; the refresh poller (successor "
        "bead) rotates the tokens and stamps last_refresh_at/last_refresh_error."
    ),
    "nouns": [
        "codex credentials", "chatgpt oauth", "codex auth.json",
        "codex install", "refresh token", "id token", "account id",
    ],
    "related_set_ids": [
        "dashboard.claude.credentials#1",
    ],
}


#: The operator's own store, and only there. These hold credentials for
#: accounts the operator owns, not an organization -- every writer already
#: names `personal` by a module constant, and the launcher reads it back
#: the same way. Declared so it is ENFORCED rather than agreed: an
#: undeclared home cannot refuse a write into an organization's database,
#: and leaves a bare `graph set members` looking in the caller's own
#: store and reporting "(no Settings)" for rows that plainly exist.
#: Band-pinned for the same reason its sibling ``dashboard.claude.credentials``
#: is: the payload carries an OAuth token triple. Without this the band resolves
#: to the full range, and two things follow that nobody chose — the row can be
#: promoted to a peer-visible state, and ``read_set`` opens peer databases for
#: this set at all, because whether peers compose is derived from exactly this
#: declaration (``settings_ops`` drops peers when the band cannot reach a
#: peer-visible state). A secret at ``published`` is the one real read-side leak
#: the rubric names, and the sibling set was already pinned against it.
@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="account_uuid")
class CodexCredentialsV1(SettingSchema):
    """Per-account Codex OAuth credentials.

    Key: ChatGPT ``account_id`` (from ``tokens.account_id`` in
    ``auth.json``). Payload carries the ChatGPT-mode token triple plus
    the id_token's derived expiry and the poller's freshness metadata.

    ``auth_mode`` is retained because the launcher and successor poller
    branch on it (``chatgpt`` vs the API-key path); API-key credentials
    are *not* stored here — they carry no ``account_id`` and no OAuth
    tokens, so they have no row on this account-keyed surface.
    """

    set_id = CODEX_CREDENTIALS_SET_ID
    schema_revision = CODEX_CREDENTIALS_REVISION

    email: str | None = field(
        default=None,
        description=(
            "Account email from the id_token claims. Display convenience "
            "only — the identity key is the ChatGPT account_id, so a bundle "
            "whose id_token omits the email claim still stores cleanly with "
            "email null."
        ),
    )
    auth_mode: str = field(
        required=True,
        description=(
            "Codex auth mode from auth.json (``chatgpt`` for the OAuth "
            "bundles stored here). The launcher/poller branch on it."
        ),
    )
    access_token: str = field(
        required=True,
        description=(
            "Current ChatGPT OAuth access token. Codex self-refreshes it on "
            "launch today; the successor refresh poller will rotate it here."
        ),
    )
    refresh_token: str = field(
        required=True,
        description=(
            "ChatGPT OAuth refresh token — the true liveness gate, since "
            "Codex refreshes the access token on next launch from it."
        ),
    )
    id_token: str = field(
        required=True,
        description=(
            "ChatGPT OpenID id_token (a JWT). Carries the account identity "
            "and the signed ``exp`` used to compute ``expires_at_ms``."
        ),
    )
    expires_at_ms: int = field(
        required=True,
        description=(
            "id_token expiry in epoch milliseconds, from the JWT ``exp`` "
            "claim (``exp * 1000``). Used for the idempotent freshness "
            "comparison on re-import."
        ),
    )
    last_refresh_at: str | None = field(
        default=None,
        description=(
            "ISO-8601 timestamp of the most recent refresh by the poller. "
            "Absent until the successor refresh poller runs; import writes "
            "the bundle clean without it."
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
            "auth_mode", "access_token", "refresh_token", "id_token",
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
        for opt in ("email", "last_refresh_at", "last_refresh_error"):
            v = payload.get(opt)
            if v is not None and not isinstance(v, str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {opt!r} must be a string or null"
                )
