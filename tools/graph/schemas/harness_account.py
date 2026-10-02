"""Harness accounts as three sets keyed ``<harness>:<account_id>`` (auto-raepo).

Design of record: the auto-raepo comment of 2026-10-02 14:14Z, approved by
the operator. An account is described by PUBLIC data that enumeration and
selection read without touching the vault; its secrets live in one vaulted
row, read only after the account has been chosen.

1. ``autonomy.harness.account`` -- public, unencrypted, a typed union per
   harness: identity, label and the state of its credential.
2. ``dashboard.harness.usage`` -- the existing usage cache, keyed the same.
3. ``autonomy.vault.harness-credential`` -- vaulted (audited), secrets only,
   a typed union per harness. The vault seals the whole payload.

The organization-homed equivalents of 1 and 3 have the same shapes and keys.
Fields the design writes as ``X | None`` are declared ``field(required=False)``
(the schema convention; None and absent both pass).
"""
from __future__ import annotations

from typing import Literal

from tools.graph.schemas.registry import (
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    payload_union,
    publication_band,
    vaulted,
)

HARNESS_ACCOUNT_SET_ID = "autonomy.harness.account"
HARNESS_CREDENTIAL_SET_ID = "autonomy.vault.harness-credential"
ORG_HARNESS_ACCOUNT_SET_ID = "autonomy.org.harness.account"
ORG_HARNESS_CREDENTIAL_SET_ID = "autonomy.org.vault.harness-credential"
HARNESS_ACCOUNT_REVISION = 1
KEY_STRATEGY = "harness:account_id"

CREDENTIAL_STATES = ("ok", "expired", "refresh_failed", "missing")

SYNOPSIS = {
    "summary": (
        "Harness accounts: public account data (set 1) readable without the "
        "vault, and one vaulted credential per account (set 3), both keyed "
        "<harness>:<account_id>."
    ),
    "nouns": ["harness account", "claude account", "codex account",
              "grok account", "harness credential"],
    "related_set_ids": ["dashboard.harness.usage#1"],
}


# ── set 1 shapes: public ─────────────────────────────────────


class _AccountCommon(SettingSchema):
    account_id: str = field(required=True, description="The account's identity within its harness.")
    alias: str = field(required=False, description="The operator's label for the account.")
    credential_state: str = field(
        required=True, enum=CREDENTIAL_STATES,
        description="Whether the account's credential can be used: ok, expired, "
                    "refresh_failed or missing.",
    )
    credential_expires_at: int = field(
        required=False,
        description="Epoch ms after which the credential cannot be used. For Claude, "
                    "the later of access_expires_at and setup_expires_at.",
    )
    refreshed_at: str = field(required=False, description="ISO 8601 time of the last refresh.")
    error: str = field(required=False, description="The last refresh error, when one happened.")


class ClaudeAccount(_AccountCommon):
    harness: Literal["claude"] = field(required=True, description="The harness.")
    email: str = field(required=False, description="The account's sign-in email.")
    org_name: str = field(required=False, description="The Claude organization's name.")
    scopes: list[str] = field(required=False, description="OAuth scopes granted.")
    access_expires_at: int = field(
        required=False, description="Epoch ms the OAuth access token expires.")
    setup_expires_at: int = field(
        required=False, description="Epoch ms the setup token expires (minted + 365 days).")


class CodexAccount(_AccountCommon):
    harness: Literal["codex"] = field(required=True, description="The harness.")
    email: str = field(required=False, description="The account's sign-in email.")


class GrokAccount(_AccountCommon):
    harness: Literal["grok"] = field(required=True, description="The harness.")


ACCOUNT_SHAPES = (ClaudeAccount, CodexAccount, GrokAccount)


# ── set 3 shapes: secrets only ───────────────────────────────


class ClaudeCredential(SettingSchema):
    harness: Literal["claude"] = field(required=True, description="The harness.")
    setup: str = field(required=False, description="The minted setup token.")
    access: str = field(required=False, description="The OAuth access token.")
    refresh: str = field(required=False, description="The OAuth refresh token.")


class CodexCredential(SettingSchema):
    harness: Literal["codex"] = field(required=True, description="The harness.")
    id_token: str = field(required=True, description="The signed identity token.")
    access: str = field(required=True, description="The access token.")
    refresh: str = field(required=True, description="The refresh token.")


class GrokCredential(SettingSchema):
    harness: Literal["grok"] = field(required=True, description="The harness.")
    auth: str = field(required=True, description="The stored sign-in file, verbatim.")


CREDENTIAL_SHAPES = (ClaudeCredential, CodexCredential, GrokCredential)


# ── the sets ─────────────────────────────────────────────────


@payload_union(discriminator="harness", shapes=ACCOUNT_SHAPES)
@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy=KEY_STRATEGY)
class HarnessAccountV1(SettingSchema):
    """An account in the operator's own store: public, unencrypted."""
    set_id = HARNESS_ACCOUNT_SET_ID
    schema_revision = HARNESS_ACCOUNT_REVISION


@payload_union(discriminator="harness", shapes=CREDENTIAL_SHAPES)
@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy=KEY_STRATEGY)
@vaulted("audited")
class HarnessCredentialV1(SettingSchema):
    """An account's secrets in the operator's own vault, sealed whole."""
    set_id = HARNESS_CREDENTIAL_SET_ID
    schema_revision = HARNESS_ACCOUNT_REVISION


@payload_union(discriminator="harness", shapes=ACCOUNT_SHAPES)
@home("organization")
@publication_band(max="raw")
@keyed_per_entity(key_strategy=KEY_STRATEGY)
class OrgHarnessAccountV1(SettingSchema):
    """An organization-shared account: public, unencrypted, any member reads it."""
    set_id = ORG_HARNESS_ACCOUNT_SET_ID
    schema_revision = HARNESS_ACCOUNT_REVISION


@payload_union(discriminator="harness", shapes=CREDENTIAL_SHAPES)
@home("organization")
@publication_band(max="raw")
@keyed_per_entity(key_strategy=KEY_STRATEGY)
@vaulted("audited")
class OrgHarnessCredentialV1(SettingSchema):
    """An organization-shared account's secrets, sealed to the organization's
    key generations: any current member opens it."""
    set_id = ORG_HARNESS_CREDENTIAL_SET_ID
    schema_revision = HARNESS_ACCOUNT_REVISION
