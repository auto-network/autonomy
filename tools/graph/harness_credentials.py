"""Harness accounts: the one module that names their rows (auto-raepo).

Design of record: the auto-raepo comment of 2026-10-02 14:14Z. An account is
two rows keyed ``<harness>:<account_id>``:

* its PUBLIC row in ``autonomy.harness.account`` (identity, label, the
  state and expiry of its credential), readable without the vault;
* its CREDENTIAL in ``autonomy.vault.harness-credential``: the account's
  secrets as ONE sealed value, replaced whole on every change.

An organization-shared account uses the organization-homed equivalents of
both sets (``--org``). The generic vault sets hold single-value secrets; a
multi-part credential has its own defined set, which is this one.

Every writer (the install command, the Getting Started scan, the refresh
pollers) and every reader (the launcher, the usage probe, the sessions
store, the account commands) goes through this module, in one vocabulary of
parts (``access``, ``refresh``, ``expires``, ``alias``, ...) that it maps
onto the two rows; nothing else names a row.

A credential seals cold, to the delegate recipient the node publishes; it
opens only while the operator is unlocked, which is when sessions launch and
pollers run. A row that is present but not openable reports as such rather
than as absent. Reads go to wherever the vault is open: a process holding
the delegate key reads the local store directly; a container asks the
dashboard. Writes seal cold: a container writes through the dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from tools.graph.schemas.harness_account import (
    HARNESS_ACCOUNT_SET_ID,
    HARNESS_CREDENTIAL_SET_ID,
    ORG_HARNESS_ACCOUNT_SET_ID,
    ORG_HARNESS_CREDENTIAL_SET_ID,
)
from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID

logger = logging.getLogger(__name__)

#: The dispatcher's scoped bearer, minted by the dashboard at every start into
#: its ramfs key cache -- never the data volume (auto-es7ja) -- and read per
#: call so a rotation takes effect without restarting the dispatcher
#: (agents/dispatcher._monitor_service_token).
DISPATCH_TOKEN_RELEASE = ("dispatcher", "token")


def dispatch_token_path() -> str:
    """Where the dispatcher's token is: AUTONOMY_DISPATCH_TOKEN_FILE, else
    <keycache>/dispatcher/token (the dashboard and the dispatcher both see
    the key cache at the same path)."""
    override = os.environ.get("AUTONOMY_DISPATCH_TOKEN_FILE")
    if override:
        return override
    root = os.environ.get("AUTONOMY_KEYCACHE_MOUNT") or "/run/autonomy-keycache"
    return os.path.join(root, *DISPATCH_TOKEN_RELEASE)

HARNESSES = ("claude", "codex", "grok")
#: The source of an account in the operator's own store.
PERSONAL = "personal"

# The vocabulary callers read and write (``Account.get`` / ``write_account``),
# mapped onto the two sets: each part is either a SECRET field of the
# account's vaulted credential (set 3) or a PUBLIC field of its account row
# (set 1). The mapping is the only place either is named.
CLAUDE_PARTS = (
    "setup", "setup_minted_at", "access", "refresh", "expires", "scopes",
    "alias", "email", "org_name", "refreshed_at", "error",
)
CODEX_PARTS = ("id", "access", "refresh", "expires", "email", "refreshed_at", "error")
GROK_PARTS = ("auth", "alias")
PARTS = {"claude": CLAUDE_PARTS, "codex": CODEX_PARTS, "grok": GROK_PARTS}

#: part -> the credential field holding it (everything else is public).
SECRET_PARTS = {
    "claude": {"setup": "setup", "access": "access", "refresh": "refresh"},
    "codex": {"id": "id_token", "access": "access", "refresh": "refresh"},
    "grok": {"auth": "auth"},
}
#: part -> public field, where the name differs or the value converts.
_PUBLIC_RENAMES = {
    "claude": {"expires": "access_expires_at", "setup_minted_at": "setup_expires_at"},
    "codex": {"expires": "credential_expires_at"},
    "grok": {},
}

#: What makes an account launchable, per harness.
CLAUDE_BUNDLE = ("access", "refresh")
CODEX_REQUIRED = ("id", "access", "refresh")
GROK_REQUIRED = ("auth",)

#: Retained for callers that compare against it; a cleared part is None.
NONE = "-"

#: A Claude setup token lives one year from minting.
SETUP_TOKEN_TTL = timedelta(days=365)


def account_key(harness: str, account_id: str, part: str | None = None) -> str:
    """The two sets' key for an account, ``<harness>:<account_id>``. *part*,
    when given, is checked against the harness's vocabulary."""
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness {harness!r}")
    if not account_id or ":" in account_id or "." in account_id:
        raise ValueError(f"account id {account_id!r} cannot key a row")
    if part is not None and part not in PARTS[harness]:
        raise ValueError(f"unknown {harness} part {part!r}")
    return f"{harness}:{account_id}"


def _prefix(harness: str) -> str:
    return f"{harness}:"


def _sets_for(org: str | None) -> tuple[str, str]:
    """(public account set, vaulted credential set) for the operator's own
    store or an organization's shared one."""
    if org is None:
        return HARNESS_ACCOUNT_SET_ID, HARNESS_CREDENTIAL_SET_ID
    return ORG_HARNESS_ACCOUNT_SET_ID, ORG_HARNESS_CREDENTIAL_SET_ID


def _iso_to_ms(text: str | None) -> int | None:
    dt = parse_iso(text)
    return int(dt.timestamp() * 1000) if dt is not None else None


def _ms_to_iso(ms: int | None) -> str | None:
    if not isinstance(ms, int):
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Account:
    """One account: its public row (set 1) and its opened credential
    (set 3). ``parts``, when given, describes it in the caller vocabulary
    and is mapped exactly as :func:`write_account` maps a write."""

    def __init__(self, harness: str, id: str, parts: dict[str, Any] | None = None, *,
                 public: dict[str, Any] | None = None,
                 secret: dict[str, Any] | None = None,
                 openable: bool = True, public_id: str | None = None,
                 credential_id: str | None = None, source: str = PERSONAL) -> None:
        self.harness = harness
        self.id = id
        self.public = dict(public or {})
        self.secret = dict(secret or {})
        #: False when the credential row is present but could not be opened
        #: (the vault is cold).
        self.openable = openable
        self.public_id = public_id
        self.credential_id = credential_id
        #: "personal" (the operator's own store) or the slug of the
        #: organization whose shared sets hold it (auto-26e8a).
        self.source = source
        if parts:
            self.apply(parts)

    def __repr__(self) -> str:   # never the secrets
        return f"Account({self.harness!r}, {self.id!r}, source={self.source!r})"

    def apply(self, parts: dict[str, Any]) -> None:
        """Set *parts* (the caller vocabulary; None clears) on this record."""
        for part, value in parts.items():
            secret_field = SECRET_PARTS[self.harness].get(part)
            if secret_field is not None:
                if value in (None, "", NONE):
                    self.secret.pop(secret_field, None)
                else:
                    self.secret[secret_field] = str(value)
                continue
            name, converted = _public_value(self.harness, part, value)
            if converted is None:
                self.public.pop(name, None)
            else:
                self.public[name] = converted

    def get(self, part: str) -> str | None:
        """A part in the caller vocabulary, as text (or None)."""
        secret_field = SECRET_PARTS[self.harness].get(part)
        if secret_field is not None:
            v = self.secret.get(secret_field)
            return v if isinstance(v, str) and v else None
        name = _PUBLIC_RENAMES[self.harness].get(part, part)
        v = self.public.get(name)
        if v is None:
            return None
        if part == "setup_minted_at":
            return _ms_to_iso(v - int(SETUP_TOKEN_TTL.total_seconds() * 1000))
        if part == "scopes":
            return scopes_text(v) if isinstance(v, list) else None
        if isinstance(v, int):
            return str(v)
        return v if isinstance(v, str) and v else None

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
        """The setup token is present and not past setup_expires_at."""
        if self.get("setup") is None:
            return False
        expires = self.public.get("setup_expires_at")
        if not isinstance(expires, int):
            return True
        return expires > int((now or datetime.now(timezone.utc)).timestamp() * 1000)


# ── the seat: dashboard client or the local store ────────────


def _in_container() -> bool:
    return bool(os.environ.get("GRAPH_API"))


def _vault_open_here() -> bool:
    """True when this process holds the operator's delegate key."""
    from tools.graph import settings_ops
    return getattr(settings_ops, "_personal_delegate_audited_key", None) is not None


def _bearer() -> str | None:
    """The bearer a process uses to ask the dashboard: the dispatcher's own
    scoped token when this process has one, else the session token in the
    environment. Never an inherited session token when the scoped one
    exists: that one is revoked with its session."""
    try:
        with open(dispatch_token_path(), "r", encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass
    return os.environ.get("CROSSTALK_TOKEN") or None


def _org_vault_open_here() -> bool:
    """True when this process can open organization vault rows."""
    from tools.graph import settings_ops
    return getattr(settings_ops, "_vault_key_holder", None) is not None


def _read_via_dashboard(set_id: str, prefix: str, org: str | None = None) -> list[Any]:
    """GET *set_id*'s rows under *prefix* from the dashboard, which opens
    only those rows."""
    from tools.graph.client import _dict_to_resolved_setting

    api = os.environ.get("GRAPH_API") or "https://localhost:8080"
    headers = {"Accept": "application/json"}
    if org is not None:
        headers["X-Graph-Org"] = org
    token = _bearer()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"{api}/api/graph/settings/{set_id}?peers=&"
        + urllib.parse.urlencode({"key_prefix": prefix}), headers=headers,
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        body = json.load(resp)
    return [_dict_to_resolved_setting(m) for m in body.get("members", [])]


def _read_all(set_id: str, read_set: Callable[..., Any] | None = None, *, prefix: str,
              org: str | None = None) -> list[Any]:
    """*set_id*'s rows under *prefix* (one harness's accounts). The prefix
    filters in the query, before the vault opens anything, so no other
    secret is decrypted to list these."""
    from tools.graph import ops as graph_ops

    if read_set is not None:
        members = read_set(set_id, org=org, peers=[], key_prefix=prefix)
        return list(getattr(members, "members", []) or [])
    if (_org_vault_open_here() or not _in_container()) if org is not None else (
            _vault_open_here() or not _in_container()):
        members = graph_ops.read_set(set_id, org=org, peers=[], key_prefix=prefix)
        return list(getattr(members, "members", []) or [])
    return _read_via_dashboard(set_id, prefix, org)


def _write(set_id: str, key: str, payload: dict, org: str | None = None) -> str:
    """Write *payload* as the row for *key*, replacing any row it had (a
    vault value is replaced whole, never stacked: auto-z4582). A container
    writes through the dashboard's setting-write route, which is
    write_by_key."""
    from tools.graph.schemas.harness_account import HARNESS_ACCOUNT_REVISION
    if _in_container():
        from tools.graph.client import get_client
        return get_client().add_setting(
            set_id, HARNESS_ACCOUNT_REVISION, key, payload, org=org, state="raw",
        )
    from tools.graph import ops as graph_ops
    return graph_ops.write_by_key(
        set_id, HARNESS_ACCOUNT_REVISION, key, payload, org=org, state="raw",
    )


def _remove(row_id: str, org: str | None = None) -> None:
    if _in_container():
        from tools.graph.client import get_client
        get_client().remove_setting(row_id, org=org)
        return
    from tools.graph import ops as graph_ops
    graph_ops.remove_setting(row_id, org=org)


# ── reading ──────────────────────────────────────────────────


def _payload(row: Any) -> dict[str, Any] | None:
    if getattr(row, "vault_error", None) is not None:
        return None
    payload = getattr(row, "payload", None)
    return payload if isinstance(payload, dict) else None


def list_accounts(
    harness: str, *, read_set: Callable[..., Any] | None = None,
    org: str | None = None,
) -> list[Account]:
    """Every account of *harness* in the operator's store, or with *org* in
    that organization's shared sets, sorted by id: the public row joined
    with the account's opened credential."""
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness {harness!r}")
    account_set, credential_set = _sets_for(org)
    prefix = _prefix(harness)
    try:
        public_rows = _read_all(account_set, read_set, prefix=prefix, org=org)
        credential_rows = _read_all(credential_set, read_set, prefix=prefix, org=org)
    except Exception:
        logger.exception("harness accounts: could not read %s accounts", harness)
        return []
    by_id: dict[str, Account] = {}
    for row in public_rows:
        key = getattr(row, "key", None)
        payload = _payload(row)
        if not isinstance(key, str) or not key.startswith(prefix) or payload is None:
            continue
        account_id = key[len(prefix):]
        by_id[account_id] = Account(harness, account_id, public=dict(payload),
                                    public_id=getattr(row, "id", None),
                                    source=org or PERSONAL)
    for row in credential_rows:
        key = getattr(row, "key", None)
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        acct = by_id.get(key[len(prefix):])
        if acct is None:
            continue          # a credential with no account row is not listed
        acct.credential_id = getattr(row, "id", None)
        payload = _payload(row)
        if payload is None:
            acct.openable = False
            continue
        acct.secret = {k: v for k, v in payload.items() if k != "harness"}
    return [by_id[k] for k in sorted(by_id)]


def read_account(
    harness: str, account_id: str, *, read_set: Callable[..., Any] | None = None,
    org: str | None = None,
) -> Account | None:
    for acct in list_accounts(harness, read_set=read_set, org=org):
        if acct.id == account_id:
            return acct
    return None


def organization_slugs() -> list[str]:
    """The organizations whose shared accounts this machine can list: every
    organization store it holds."""
    from tools.graph import org_ops
    try:
        return [ref.slug for ref in org_ops.list_orgs()
                if ref.slug not in (PERSONAL, "machine")]
    except Exception:
        return []


def all_accounts(harness: str, *, orgs: Iterable[str] | None = None) -> list[Account]:
    """Personal accounts, then each organization's shared accounts (every
    organization store this machine holds unless *orgs* names them), each
    marked by ``source``."""
    out = list_accounts(harness)
    for slug in (organization_slugs() if orgs is None else orgs):
        out.extend(list_accounts(harness, org=slug))
    return out


def find_account(harness: str, *, alias: str) -> Account | None:
    for acct in list_accounts(harness):
        if acct.get("alias") == alias:
            return acct
    return None


# ── writing ──────────────────────────────────────────────────


def _public_value(harness: str, part: str, value: str | None) -> tuple[str, Any]:
    name = _PUBLIC_RENAMES[harness].get(part, part)
    if value is None or value == NONE or value == "":
        return name, None
    if part == "expires":
        return name, expires_ms(value)
    if part == "setup_minted_at":
        minted = _iso_to_ms(value)
        return name, (minted + int(SETUP_TOKEN_TTL.total_seconds() * 1000)
                      if minted is not None else None)
    if part == "scopes":
        return name, scopes_list(value) or None
    return name, str(value)


def _credential_state(acct: Account, now_ms: int) -> tuple[str, int | None]:
    """(credential_state, credential_expires_at) derived from the account."""
    if acct.harness == "claude":
        candidates = [v for v in (acct.public.get("access_expires_at"),
                                  acct.public.get("setup_expires_at"))
                      if isinstance(v, int)]
        expires = max(candidates) if candidates else None
    else:
        expires = acct.public.get("credential_expires_at")
        expires = expires if isinstance(expires, int) else None
    if not acct.launchable:
        return "missing", expires
    if acct.public.get("error"):
        return "refresh_failed", expires
    if expires is not None and expires <= now_ms and not (
            acct.harness == "claude" and acct.has(*CLAUDE_BUNDLE)):
        # an expired access token with a refresh token is refreshable, so ok
        return "expired", expires
    return "ok", expires


def write_account(harness: str, account_id: str, parts: dict[str, str | None],
                  *, org: str | None = None) -> Account:
    """Apply *parts* (the caller vocabulary; ``None`` clears a part) to the
    account: its credential row (set 3) is written first, as one sealed
    value replacing the previous one, then its public row (set 1), with the
    credential's state and expiry derived. A rotation is therefore one
    write of the credential."""
    key = account_key(harness, account_id)
    for part in parts:
        account_key(harness, account_id, part)
    existing = (read_account(harness, account_id, org=org)
                or Account(harness, account_id, source=org or PERSONAL))
    secret_changes = {SECRET_PARTS[harness][p]: v for p, v in parts.items()
                      if p in SECRET_PARTS[harness]}
    if secret_changes and existing.credential_id and not existing.openable:
        raise RuntimeError(
            f"{harness} account {account_id}: the credential cannot be opened "
            "(the vault is cold), so it cannot be changed")
    account_set, credential_set = _sets_for(org)
    existing.apply(parts)
    if secret_changes:
        existing.credential_id = _write(credential_set, key,
                                        {"harness": harness, **existing.secret}, org)
    public = existing.public
    if existing.openable:
        state, expires = _credential_state(
            existing, int(datetime.now(timezone.utc).timestamp() * 1000))
        if expires is None:
            public.pop("credential_expires_at", None)
        else:
            public["credential_expires_at"] = expires
    else:
        # The credential did not open (cold vault), so its state cannot be
        # judged here: keep what the last warm write recorded.
        state = public.get("credential_state") or "missing"
    public.update({"harness": harness, "account_id": account_id, "credential_state": state})
    existing.public_id = _write(account_set, key, public, org)
    return existing


def remove_account(harness: str, account_id: str, *, org: str | None = None) -> int:
    """Remove the account's rows; returns how many rows went."""
    acct = read_account(harness, account_id, org=org)
    if acct is None:
        return 0
    count = 0
    for row_id in (acct.credential_id, acct.public_id):
        if row_id:
            _remove(row_id, org)
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


#: The fields of a retired pre-vault row that are credentials. Everything
#: else (alias, account_email, organization_name, expires_at_ms, ...) stays
#: for diagnosis. ``id_token`` is a signed identity token, so it goes too.
RETIRED_SECRET_FIELDS = ("access_token", "refresh_token", "raw_key", "id_token")

#: Each retired set, the account harness it migrated into, and the account
#: part whose presence in the vault confirms the copy.
_RETIRED_SETS = (
    ("dashboard.claude.credentials", "claude", "refresh"),
    ("dashboard.claude.setup_tokens", "claude", "setup"),
    ("dashboard.codex.credentials", "codex", "refresh"),
)


def migrate_plaintext_accounts() -> dict[str, int]:
    """Seal every pre-vault credential row into its account record and
    deprecate the row (record v16 §10.9), then erase the secrets of every
    migrated row whose vault copy is confirmed (auto-se3e2).

    Runs at every dashboard startup. A row is sealed and deprecated once;
    its secrets are erased on the same run or, for rows migrated before the
    erasure existed, on the next. A row whose vault write fails is left
    exactly as it was, secrets included, and is retried next startup; one
    failure does not stop the others. A second run changes nothing. The
    three sets have no registered schema any more, so their rows are read
    as they are and never written through the schema path again.
    """
    from tools.graph import ops as graph_ops

    counts = {"claude": 0, "setup_tokens": 0, "codex": 0, "deprecated": 0,
              "scrubbed": 0, "failed": 0}
    def _rows(set_id: str) -> list[Any]:
        try:
            return list(getattr(graph_ops.read_set(set_id, org="personal", peers=[]), "members", []) or [])
        except Exception:
            return []

    def _migrate(row: Any, harness: str, parts: dict[str, str | None], counter: str) -> None:
        try:
            write_account(harness, str(row.key), parts)
        except Exception:
            logger.exception("harness accounts: vault write for pre-vault row %s "
                             "failed; the row is kept as it was", row.id)
            counts["failed"] += 1
            return
        counts[counter] += 1
        graph_ops.deprecate_setting(row.id, org="personal")
        counts["deprecated"] += 1

    for row in _rows("dashboard.claude.credentials"):
        payload = getattr(row, "payload", None) or {}
        if not isinstance(payload, dict) or not payload.get("refresh_token"):
            continue
        expires = payload.get("expires_at_ms")
        _migrate(row, "claude", {
            "alias": payload.get("alias"),
            "org_name": payload.get("organization_name"),
            "email": payload.get("account_email"),
            "access": payload.get("access_token"),
            "refresh": payload.get("refresh_token"),
            "expires": str(expires) if isinstance(expires, int) else None,
            "scopes": scopes_text(payload.get("scopes") or []),
            "refreshed_at": payload.get("last_refresh_at"),
            "error": payload.get("last_refresh_error"),
        }, "claude")
    for row in _rows("dashboard.claude.setup_tokens"):
        payload = getattr(row, "payload", None) or {}
        raw_key = payload.get("raw_key") if isinstance(payload, dict) else None
        if not raw_key:
            continue
        _migrate(row, "claude", {
            "setup": raw_key,
            "setup_minted_at": str(getattr(row, "created_at", None) or NONE),
        }, "setup_tokens")
    for row in _rows("dashboard.codex.credentials"):
        payload = getattr(row, "payload", None) or {}
        if not isinstance(payload, dict) or not payload.get("refresh_token"):
            continue
        expires = payload.get("expires_at_ms")
        _migrate(row, "codex", {
            "id": payload.get("id_token"),
            "access": payload.get("access_token"),
            "refresh": payload.get("refresh_token"),
            "expires": str(expires) if isinstance(expires, int) else None,
            "email": payload.get("email"),
            "refreshed_at": payload.get("last_refresh_at"),
            "error": payload.get("last_refresh_error"),
        }, "codex")
    counts["scrubbed"] = scrub_migrated_secrets()
    return counts


def scrub_migrated_secrets() -> int:
    """Erase the credential fields of every retired row whose migration is
    done: the row is deprecated (the migration's mark that its vault write
    succeeded) AND the vault account holds that credential now. Returns how
    many rows were erased; a row already erased is a no-op.

    The erase overwrites the payload in place rather than deleting the row:
    the non-secret fields (alias, account email, organization, expiry) stay
    for a later diagnosis of which account a node once held, the deprecation
    stays as the audit mark of the migration, and fleet sync carries the
    erased payload to every peer as an ordinary update
    (settings_ops.erase_payload_fields; a signed row is removed instead).
    """
    from tools.graph import settings_ops as _so

    erased = 0
    for set_id, harness, part in _RETIRED_SETS:
        try:
            rows = _so.rows_including_deprecated(set_id, org="personal")
        except Exception:
            logger.exception("harness accounts: cannot read retired set %s", set_id)
            continue
        for row in rows:
            payload = row["payload"]
            if not row["deprecated"] or not isinstance(payload, dict):
                continue
            if not any(payload.get(f) for f in RETIRED_SECRET_FIELDS):
                continue
            try:
                account = read_account(harness, str(row["key"]))
            except Exception:
                account = None
            if account is None or account.get(part) is None:
                # Not confirmed (vault cold, or no copy): keep it, retry later.
                continue
            try:
                if _so.erase_payload_fields(row["id"], RETIRED_SECRET_FIELDS,
                                            org="personal") != "absent":
                    erased += 1
            except Exception:
                logger.exception("harness accounts: erasing the secrets of "
                                 "retired row %s failed", row["id"])
    if erased:
        # Every earlier rewrite of these rows (the deprecation included) left
        # a copy of the old payload in free space; rebuild the file so none
        # survives. Once per node: a later run erases nothing and skips this.
        try:
            _so.compact_store("personal")
        except Exception:
            logger.exception("harness accounts: compacting the personal store "
                             "after erasing retired secrets failed")
    return erased


# ── one-time fold of the per-part vault rows (auto-raepo) ────


def _part_matches(acct: Account, part: str, old: str) -> bool:
    """Whether the new record carries the old part's value."""
    if part == "setup_minted_at":
        return acct.public.get("setup_expires_at") == _public_value(acct.harness, part, old)[1]
    if part == "scopes":
        return scopes_list(acct.get("scopes")) == scopes_list(old)
    if part == "expires":
        return acct.expires_ms() == expires_ms(old)
    return acct.get(part) == old


def fold_vault_part_rows() -> dict[str, int]:
    """Fold the per-part rows ``<harness>.account.<id>.<part>`` of the
    personal audited vault into one public row and one sealed credential
    per account, verify the new record carries every part, then remove the
    part rows. No fallback read of the old layout remains.

    Needs the warm audited delegate: run where the vault is open (dashboard
    startup when warm, and after unlock). An account any of whose rows will
    not open is left exactly as it was and retried next run; a second run
    finds nothing to fold."""
    from tools.graph import ops as graph_ops

    counts = {"accounts": 0, "rows_removed": 0, "cold": 0, "failed": 0}
    for harness in HARNESSES:
        prefix = f"{harness}.account."
        try:
            rows = list(getattr(graph_ops.read_set(
                VAULT_AUDITED_SET_ID, org=None, peers=[], key_prefix=prefix),
                "members", []) or [])
        except Exception:
            logger.exception("harness accounts: could not read %s part rows", harness)
            counts["failed"] += 1
            continue
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            account_id, sep, part = str(row.key)[len(prefix):].rpartition(".")
            if sep and part in PARTS[harness]:
                grouped.setdefault(account_id, []).append((part, row))
        for account_id, items in sorted(grouped.items()):
            if any(getattr(row, "vault_error", None) is not None for _, row in items):
                counts["cold"] += 1
                continue
            parts: dict[str, str | None] = {}
            for part, row in items:
                value = (row.payload or {}).get("value")
                parts[part] = None if value in (None, "", NONE) else str(value)
            try:
                write_account(harness, account_id, parts)
                acct = read_account(harness, account_id)
                missing = [p for p, v in parts.items()
                           if v is not None and (acct is None or not _part_matches(acct, p, v))]
                if missing:
                    raise RuntimeError(f"new record does not carry {missing}")
            except Exception:
                logger.exception("harness accounts: fold of %s %s failed; its part "
                                 "rows are kept", harness, account_id)
                counts["failed"] += 1
                continue
            for _, row in items:
                graph_ops.remove_setting(row.id, org=None)
                counts["rows_removed"] += 1
            counts["accounts"] += 1
    return counts


def fold_where_due() -> dict[str, int] | None:
    """Run :func:`fold_vault_part_rows` where it is due: the vault is open in
    this process, and this machine is the Fleet tunnel server -- the same
    single-machine check the Claude refresh uses, so one machine folds and
    the others receive the result by sync. None when it does not run here."""
    if not _vault_open_here():
        return None
    try:
        from tools.network import fleet_tunnel_server
        if not fleet_tunnel_server.state().allowed:
            return None
    except Exception:
        logger.exception("harness accounts: could not tell whether this machine folds")
        return None
    counts = fold_vault_part_rows()
    if any(counts.values()):
        logger.info("harness accounts: folded the per-part vault rows %s", counts)
    return counts
