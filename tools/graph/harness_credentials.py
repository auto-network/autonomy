"""Harness accounts in the operator's vault: the one place their rows are named.

Design of record graph://5f2f5a49-00d v16 §10.9, on the vault rubric in
:mod:`tools.graph.schemas.vault_credential`: a harness account is one record
keyed by its account identity, held as sealed rows in the audited vault under
compound keys ``<harness>.account.<id>.<part>``. Every writer (the install
command, the Getting Started scan) and every reader (the launcher's picker,
the per-session credential files, the usage probe, the sessions store, the
account commands, the refresh pollers) goes through this module; nothing
else names a row.

Parts, per harness (each a single string; a vault row cannot be empty, so a
cleared part is written as :data:`NONE`):

* claude: ``setup`` (the minted setup token), ``setup_minted_at``, ``access``,
  ``refresh``, ``expires`` (epoch ms), ``scopes`` (comma-separated), ``alias``,
  ``email``, ``org_name``, ``refreshed_at``, ``error``.
* codex: ``id`` (the id token), ``access``, ``refresh``, ``expires`` (epoch
  ms), ``email``, ``refreshed_at``, ``error``. The account id is the key.
* grok: ``auth`` (the stored sign-in file, verbatim), ``alias``.

A personal audited row seals cold, to the delegate recipient the node
publishes at first run and identity creation; it opens only while the
operator is unlocked, which is when sessions launch and pollers run. A row
that is present but not openable reports as such rather than as absent.

Reads go to wherever the vault is open: a process holding the operator's
delegate key (the dashboard after unlock, a test with a warm fixture) reads
the local store directly; any other process (a host terminal, a container)
asks the dashboard, which opens rows server-side, and falls back to a direct
cold read only when the dashboard cannot be reached. Writes seal cold: a
container writes through the dashboard; every other process writes the
local store. Over HTTP no organization is named, so the server resolves the
rows' declared home.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
)

HARNESSES = ("claude", "codex", "grok")

CLAUDE_PARTS = (
    "setup", "setup_minted_at", "access", "refresh", "expires", "scopes",
    "alias", "email", "org_name", "refreshed_at", "error",
)
CODEX_PARTS = ("id", "access", "refresh", "expires", "email", "refreshed_at", "error")
GROK_PARTS = ("auth", "alias")
PARTS = {"claude": CLAUDE_PARTS, "codex": CODEX_PARTS, "grok": GROK_PARTS}

#: What makes an account launchable, per harness.
CLAUDE_BUNDLE = ("access", "refresh")
CODEX_REQUIRED = ("id", "access", "refresh")
GROK_REQUIRED = ("auth",)

#: A sealed "no value": a vault row cannot be empty, so a cleared part holds this.
NONE = "-"

#: A Claude setup token lives one year from minting (the schema's old TTL).
SETUP_TOKEN_TTL = timedelta(days=365)


def account_key(harness: str, account_id: str, part: str) -> str:
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness {harness!r}")
    if not account_id or ":" in account_id or "." in account_id:
        raise ValueError(f"account id {account_id!r} cannot key a vault row")
    if part not in PARTS[harness]:
        raise ValueError(f"unknown {harness} part {part!r}")
    return f"{harness}.account.{account_id}.{part}"


def _prefix(harness: str) -> str:
    return f"{harness}.account."


@dataclass
class Account:
    harness: str
    id: str
    parts: dict[str, str] = field(default_factory=dict)
    #: False when at least one row is present but could not be opened
    #: (the vault is cold); ``parts`` then holds only what did open.
    openable: bool = True
    row_ids: dict[str, str] = field(default_factory=dict)

    def get(self, part: str) -> str | None:
        v = self.parts.get(part)
        return None if v is None or v == NONE else v

    def has(self, *parts: str) -> bool:
        return all(self.get(p) is not None for p in parts)

    @property
    def launchable(self) -> bool:
        if self.harness == "claude":
            return self.setup_token_fresh() or self.has(*CLAUDE_BUNDLE)
        if self.harness == "codex":
            return self.has(*CODEX_REQUIRED)
        return self.has(*GROK_REQUIRED)

    def expires_ms(self) -> int | None:
        return expires_ms(self.get("expires"))

    def setup_token_fresh(self, now: datetime | None = None) -> bool:
        """The setup token is present and younger than a year."""
        if self.get("setup") is None:
            return False
        minted = parse_iso(self.get("setup_minted_at"))
        if minted is None:
            return True
        return minted + SETUP_TOKEN_TTL > (now or datetime.now(timezone.utc))


# ── the seat: dashboard client or the local store ────────────


def _in_container() -> bool:
    return bool(os.environ.get("GRAPH_API"))


def _vault_open_here() -> bool:
    """True when this process holds the operator's delegate key."""
    from tools.graph import settings_ops
    return getattr(settings_ops, "_personal_delegate_audited_key", None) is not None


def _read_all(read_set: Callable[..., Any] | None = None) -> list[Any]:
    from tools.graph import ops as graph_ops

    if read_set is not None:
        members = read_set(VAULT_AUDITED_SET_ID, org=None, peers=[])
    elif _vault_open_here():
        members = graph_ops.read_set(VAULT_AUDITED_SET_ID, org=None, peers=[])
    else:
        try:
            from tools.graph.client import get_client
            members = get_client().read_set(VAULT_AUDITED_SET_ID, org=None, peers=[])
        except Exception:
            if _in_container():
                raise
            members = graph_ops.read_set(VAULT_AUDITED_SET_ID, org=None, peers=[])
    return list(getattr(members, "members", []) or [])


def _write(key: str, value: str, existing_id: str | None) -> str:
    payload = {"value": value}
    if _in_container():
        from tools.graph.client import get_client
        client = get_client()
        if existing_id:
            return client.override_setting(existing_id, payload, org=None, state="raw")
        return client.add_setting(
            VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION, key, payload,
            org=None, state="raw",
        )
    from tools.graph import ops as graph_ops
    if existing_id:
        return graph_ops.override_setting(existing_id, payload, org=None, state="raw")
    return graph_ops.add_setting(
        VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION, key, payload,
        org=None, state="raw",
    )


def _remove(row_id: str) -> None:
    if _in_container():
        from tools.graph.client import get_client
        get_client().remove_setting(row_id, org=None)
        return
    from tools.graph import ops as graph_ops
    graph_ops.remove_setting(row_id, org=None)


# ── reading ──────────────────────────────────────────────────


def _value(row: Any) -> str | None:
    if getattr(row, "vault_error", None) is not None:
        return None
    payload = getattr(row, "payload", None)
    v = payload.get("value") if isinstance(payload, dict) else None
    return v if isinstance(v, str) and v else None


def list_accounts(
    harness: str, *, read_set: Callable[..., Any] | None = None,
) -> list[Account]:
    """Every account of *harness* in the vault, sorted by id."""
    prefix = _prefix(harness)
    by_id: dict[str, Account] = {}
    try:
        rows = _read_all(read_set)
    except Exception:
        return []
    for row in rows:
        key = getattr(row, "key", None)
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        account_id, sep, part = rest.rpartition(".")
        if not sep or part not in PARTS[harness]:
            continue
        acct = by_id.setdefault(account_id, Account(harness, account_id))
        row_id = getattr(row, "id", None)
        if isinstance(row_id, str):
            acct.row_ids[part] = row_id
        v = _value(row)
        if v is None:
            if getattr(row, "vault_error", None) is not None:
                acct.openable = False
            continue
        acct.parts[part] = v
    return [by_id[k] for k in sorted(by_id)]


def read_account(
    harness: str, account_id: str, *, read_set: Callable[..., Any] | None = None,
) -> Account | None:
    for acct in list_accounts(harness, read_set=read_set):
        if acct.id == account_id:
            return acct
    return None


def find_account(harness: str, *, alias: str) -> Account | None:
    for acct in list_accounts(harness):
        if acct.get("alias") == alias:
            return acct
    return None


# ── writing ──────────────────────────────────────────────────


def write_account(harness: str, account_id: str, parts: dict[str, str | None]) -> Account:
    """Seal *parts* into the account's rows; a ``None`` clears the part.

    A vault row is an encrypted object revision and is never rewritten: the
    first value of a part is added, a change appends a revision over the
    existing row, and resolution takes the newest.
    """
    existing = read_account(harness, account_id) or Account(harness, account_id)
    for part, value in parts.items():
        key = account_key(harness, account_id, part)
        text = NONE if value is None else str(value)
        if not text:
            text = NONE
        row_id = existing.row_ids.get(part)
        new_id = _write(key, text, row_id)
        existing.row_ids[part] = row_id or new_id
        existing.parts[part] = text
    return existing


def remove_account(harness: str, account_id: str) -> int:
    """Remove every row of the account; returns how many rows went."""
    acct = read_account(harness, account_id)
    if acct is None:
        return 0
    count = 0
    for row_id in acct.row_ids.values():
        _remove(row_id)
        count += 1
    return count


# ── small conversions ────────────────────────────────────────


def expires_ms(text: str | None) -> int | None:
    try:
        return int(text) if text and text != NONE else None
    except (TypeError, ValueError):
        return None


def parse_iso(text: str | None) -> datetime | None:
    if not text or text == NONE:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def scopes_list(text: str | None) -> list[str]:
    return [s for s in (text or "").split(",") if s and s != NONE]


def scopes_text(scopes: Iterable[str]) -> str:
    joined = ",".join(s for s in scopes if s)
    return joined or NONE


# ── one-time migration of the pre-vault sets ─────────────────

PLAINTEXT_SETS = (
    "dashboard.claude.credentials",
    "dashboard.claude.setup_tokens",
    "dashboard.codex.credentials",
)


def migrate_plaintext_accounts() -> dict[str, int]:
    """Seal every pre-vault credential row into its account record and
    deprecate the row (record v16 §10.9). Runs at every dashboard startup
    and is a no-op once the rows are gone; a node that never held them
    reports zeros. The three sets have no registered schema any more, so
    their rows are read as they are and never written again.
    """
    from tools.graph import ops as graph_ops

    counts = {"claude": 0, "setup_tokens": 0, "codex": 0, "deprecated": 0}
    def _rows(set_id: str) -> list[Any]:
        try:
            return list(getattr(graph_ops.read_set(set_id, org="personal", peers=[]), "members", []) or [])
        except Exception:
            return []

    for row in _rows("dashboard.claude.credentials"):
        payload = getattr(row, "payload", None) or {}
        if not isinstance(payload, dict) or not payload.get("refresh_token"):
            continue
        expires = payload.get("expires_at_ms")
        write_account("claude", str(row.key), {
            "alias": payload.get("alias"),
            "org_name": payload.get("organization_name"),
            "email": payload.get("account_email"),
            "access": payload.get("access_token"),
            "refresh": payload.get("refresh_token"),
            "expires": str(expires) if isinstance(expires, int) else None,
            "scopes": scopes_text(payload.get("scopes") or []),
            "refreshed_at": payload.get("last_refresh_at"),
            "error": payload.get("last_refresh_error"),
        })
        counts["claude"] += 1
        graph_ops.deprecate_setting(row.id, org="personal")
        counts["deprecated"] += 1
    for row in _rows("dashboard.claude.setup_tokens"):
        payload = getattr(row, "payload", None) or {}
        raw_key = payload.get("raw_key") if isinstance(payload, dict) else None
        if not raw_key:
            continue
        write_account("claude", str(row.key), {
            "setup": raw_key,
            "setup_minted_at": str(getattr(row, "created_at", None) or NONE),
        })
        counts["setup_tokens"] += 1
        graph_ops.deprecate_setting(row.id, org="personal")
        counts["deprecated"] += 1
    for row in _rows("dashboard.codex.credentials"):
        payload = getattr(row, "payload", None) or {}
        if not isinstance(payload, dict) or not payload.get("refresh_token"):
            continue
        expires = payload.get("expires_at_ms")
        write_account("codex", str(row.key), {
            "id": payload.get("id_token"),
            "access": payload.get("access_token"),
            "refresh": payload.get("refresh_token"),
            "expires": str(expires) if isinstance(expires, int) else None,
            "email": payload.get("email"),
            "refreshed_at": payload.get("last_refresh_at"),
            "error": payload.get("last_refresh_error"),
        })
        counts["codex"] += 1
        graph_ops.deprecate_setting(row.id, org="personal")
        counts["deprecated"] += 1
    return counts
