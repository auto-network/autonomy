"""The harness sign-ins as vault rows: the one place their names live.

Design of record graph://5f2f5a49-00d v12 FR7a, and the vault rubric in
:mod:`tools.graph.schemas.vault_credential`: every secret the operator owns
is a sealed row in the audited vault, named by a key, and a credential with
several parts is several rows under compound keys. These are the keys for
the three harnesses' sign-ins, written by ``graph credentials import`` and
the Getting Started scan, opened by the session launcher at launch, and
rotated by the refresh pollers. Nothing else names them.

A personal audited row seals cold (to the delegate recipient the node
publishes at first run and identity creation) and opens only while the
operator is unlocked, which is exactly when a session launches.
"""
from __future__ import annotations

from typing import Any, Callable

from tools.graph import ops as graph_ops
from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
)

CLAUDE_ACCESS = "claude.oauth.access"
CLAUDE_REFRESH = "claude.oauth.refresh"
CLAUDE_EXPIRES = "claude.oauth.expires"        # epoch milliseconds, as text
CLAUDE_SCOPES = "claude.oauth.scopes"          # comma-separated
CLAUDE_ACCOUNT = "claude.oauth.account"        # the account email
CLAUDE_KEYS = (CLAUDE_ACCESS, CLAUDE_REFRESH, CLAUDE_EXPIRES, CLAUDE_SCOPES, CLAUDE_ACCOUNT)
CLAUDE_REQUIRED = (CLAUDE_ACCESS, CLAUDE_REFRESH)

CODEX_ID = "codex.oauth.id"
CODEX_ACCESS = "codex.oauth.access"
CODEX_REFRESH = "codex.oauth.refresh"
CODEX_ACCOUNT = "codex.oauth.account"          # the ChatGPT account id
CODEX_EXPIRES = "codex.oauth.expires"          # epoch milliseconds, as text
CODEX_KEYS = (CODEX_ID, CODEX_ACCESS, CODEX_REFRESH, CODEX_ACCOUNT, CODEX_EXPIRES)
CODEX_REQUIRED = (CODEX_ID, CODEX_ACCESS, CODEX_REFRESH, CODEX_ACCOUNT)

GROK_AUTH = "grok.auth"                        # the stored sign-in file, verbatim


def seal(key: str, value: str, *, upsert: Callable[..., Any] | None = None) -> str:
    """Seal one value under *key* in the operator's audited vault.

    A vault row is an encrypted object revision and is never rewritten: the
    first value is added, a change appends a new revision over the existing
    row (``override_setting``), and resolution takes the newest.
    ``upsert`` is a test seam standing in for both writes.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"vault row {key!r} needs a non-empty string value")
    if upsert is not None:
        return upsert(
            VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION, key, {"value": value},
            org=None, state="raw",
        )
    from tools.graph import settings_ops
    existing = rows().get(key)
    if existing is not None and getattr(existing, "id", None):
        return settings_ops.override_setting(
            existing.id, {"value": value}, org=None, state="raw",
        )
    return settings_ops.add_setting(
        VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION, key, {"value": value},
        org=None, state="raw",
    )


def rows(*, read_set: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Every audited row keyed by name, opened or not: a row whose
    ``vault_error`` is set is present but cannot be opened (vault cold)."""
    fn = read_set or graph_ops.read_set
    try:
        members = fn(VAULT_AUDITED_SET_ID, org=None, peers=[])
    except Exception:
        return {}
    return {
        getattr(m, "key", None): m
        for m in (getattr(members, "members", []) or [])
        if getattr(m, "key", None)
    }


def present(keys, *, read_set: Callable[..., Any] | None = None) -> bool:
    """True when every key in *keys* has a row, opened or not."""
    have = rows(read_set=read_set)
    return all(k in have for k in keys)


def open_value(key: str, *, read_set: Callable[..., Any] | None = None) -> str | None:
    """The plaintext under *key*, or None when absent or not openable."""
    row = rows(read_set=read_set).get(key)
    return _value(row)


def open_values(keys, *, read_set: Callable[..., Any] | None = None) -> dict[str, str]:
    """The plaintext under each of *keys* that is present and openable."""
    have = rows(read_set=read_set)
    out: dict[str, str] = {}
    for k in keys:
        v = _value(have.get(k))
        if v is not None:
            out[k] = v
    return out


def _value(row: Any) -> str | None:
    if row is None or getattr(row, "vault_error", None) is not None:
        return None
    payload = getattr(row, "payload", None)
    v = payload.get("value") if isinstance(payload, dict) else None
    return v if isinstance(v, str) and v else None


def expires_ms(text: str | None) -> int | None:
    try:
        return int(text) if text else None
    except (TypeError, ValueError):
        return None
