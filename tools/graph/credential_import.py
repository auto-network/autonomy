"""Layer-0 credential import — ``graph credentials import`` (bead auto-5bq85).

The **outer agent** (the user's own harness following ``/install``) runs
this during Stage-1 install. It discovers the user's *existing* local
Claude / Codex auth on this machine and copies the Claude bundle into the
substrate credential Settings so inner workspace sessions authenticate
without the user re-doing sign-in.

Scope (locked by the bead's boundary decisions):

* **Consume-only, never a transplant.** The user's original auth files
  (``~/.claude/.credentials.json``, ``~/.codex/auth.json``) are READ-ONLY
  inputs — never modified, moved, or deleted. We consume a *copy*;
  ownership stays with the user's harness. Secure vaulting / ownership
  transfer is vault C5, explicitly out of scope here.
* **No interactive secret prompt, ever.** Absent file = harness not
  authed = report ``needs-sign-in`` and skip. We never prompt for a
  secret. (A structural test in ``tests/test_credential_import.py`` pins
  this: the module contains no ``input()`` call.)
* **Validate before import.** One authenticated no-op per discovered
  credential; import only what passes, report what fails as
  ``needs-sign-in``. For Claude the no-op is a read-only ``GET
  /api/oauth/profile`` that both proves liveness and yields the org
  identity we key the row by. For Codex the no-op is the id_token's own
  signed expiry (a JWT ``exp`` in the future is a live-token proxy that
  needs no network round-trip).
* **Reuse the existing storage surface — no new storage, no schema
  fork.** Claude rows land in ``dashboard.claude.credentials`` (personal
  org, the org every consumer — launcher, refresh poller, usage poller —
  reads; see ``agents/session_launcher._credentials_org``). The write is
  an idempotent upsert keyed by the Anthropic org UUID: re-running
  refreshes, never duplicates.
* **Codex now has a substrate Setting too** (bead auto-kzws9, STEP 1 of
  the Codex credential end-state). ``dashboard.codex.credentials`` mirrors
  the Claude surface, keyed by the ChatGPT ``account_id``. A validated
  ChatGPT-mode bundle is copied into it with the same idempotent
  ``upsert_by_key`` + freshness rule Claude uses. This is TRANSITIONAL:
  the launcher and the read-only host mount are UNTOUCHED here — the
  cutover, the refresh poller, and mount retirement are the successor
  bead's whole job, so the credential is written to the substrate *and*
  still consumed in place via the mount until the successor lands.
  API-key-mode Codex credentials are not stored on this surface — they
  carry no ``account_id`` and no OAuth tokens — so they stay ``in-place``
  with no write.

The only substrate writes this module performs are the Claude and Codex
``upsert_by_key`` calls — nothing is written outside the Settings store.

Spec: bead auto-5bq85 (Claude), bead auto-kzws9 (Codex step 1). Storage of
record: ``graph://73c4e9ef-bbc``. Auth runbook: ``graph://ffa116fb-f85``.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from . import ops
from .claude_oauth import CLAUDE_USER_AGENT, CONSUMER_SCOPES
from .schemas.claude_credentials import (
    CLAUDE_CREDENTIALS_REVISION,
    CLAUDE_CREDENTIALS_SET_ID,
)
from .schemas.codex_credentials import (
    CODEX_CREDENTIALS_REVISION,
    CODEX_CREDENTIALS_SET_ID,
)


logger = logging.getLogger(__name__)


# The org every credential consumer reads from — mirror of
# ``agents/session_launcher._credentials_org`` and the refresh poller's
# ``_credentials_org``. Host-local secrets live in ``personal.db`` and are
# never shared cross-org, so we pin to it regardless of ambient GRAPH_ORG.
CREDENTIALS_ORG = "personal"

# Default source paths on the user's machine. Overridable via ``--home``
# so the whole flow is testable against a fixture home.
CLAUDE_CREDENTIALS_RELPATH = ".claude/.credentials.json"
CODEX_AUTH_RELPATH = ".codex/auth.json"

# Read-only authenticated no-op for a Claude consumer bundle. Returns the
# org identity (uuid / name / account email) we need to key + populate the
# credentials row, and a 2xx proves the access_token is live. Chosen over
# a refresh grant deliberately: a refresh would rotate the token and so
# invalidate the copy still sitting in the user's on-disk file — that is a
# transplant, not a consume. This GET mutates nothing.
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_OAUTH_BETA_HEADER = "oauth-2025-04-20"


class CredentialImportError(RuntimeError):
    """A discovered credential could not be parsed or validated.

    Raised for malformed files or failed validation calls. The
    orchestrator catches it and turns it into a ``needs_sign_in`` result —
    a missing file (harness simply not authed) is *not* an error and is
    signalled by discovery returning ``None`` instead.
    """


# ── result model ─────────────────────────────────────────────


# Terminal per-harness statuses. ``imported`` / ``unchanged`` describe a
# real substrate write (or a no-op because the row was already fresh);
# ``in_place`` means the credential is valid and already where the
# consumer reads it (Codex); ``would_import`` is the ``--dry-run`` echo;
# ``needs_sign_in`` covers absent / expired / invalid / unreachable.
STATUS_IMPORTED = "imported"
STATUS_UNCHANGED = "unchanged"
STATUS_IN_PLACE = "in_place"
STATUS_WOULD_IMPORT = "would_import"
STATUS_NEEDS_SIGN_IN = "needs_sign_in"


@dataclass
class HarnessResult:
    harness: str
    status: str
    detail: str
    account: str | None = None


@dataclass
class ImportReport:
    results: list[HarnessResult] = field(default_factory=list)

    def add(self, result: HarnessResult) -> None:
        self.results.append(result)


# ── Claude: discovery ────────────────────────────────────────


@dataclass
class ClaudeDiscovery:
    access_token: str
    refresh_token: str
    expires_at_ms: int
    scopes: list[str]
    subscription_type: str | None
    source_path: str


def discover_claude(home: str) -> ClaudeDiscovery | None:
    """Parse ``<home>/.claude/.credentials.json`` into a :class:`ClaudeDiscovery`.

    Returns ``None`` when the file is absent (harness simply not authed —
    report-and-skip, not an error). Raises :class:`CredentialImportError`
    when the file exists but is unreadable or its shape is unrecognized
    (e.g. the macOS keychain-backed variant, where the CLI keeps the
    bundle in the Keychain and the JSON on disk is a reference rather than
    the tokens — keychain extraction is deferred, so we surface a clear
    ``needs-sign-in``).
    """
    path = os.path.join(home, CLAUDE_CREDENTIALS_RELPATH)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialImportError(
            f"~/.claude/.credentials.json is present but unreadable: {exc}"
        ) from None
    bundle = raw.get("claudeAiOauth") if isinstance(raw, dict) else None
    if not isinstance(bundle, dict):
        raise CredentialImportError(
            "~/.claude/.credentials.json has no 'claudeAiOauth' bundle "
            "(keychain-backed logins are not yet importable)"
        )
    access_token = bundle.get("accessToken")
    refresh_token = bundle.get("refreshToken")
    expires_at = bundle.get("expiresAt")
    if not isinstance(access_token, str) or not access_token:
        raise CredentialImportError(
            "~/.claude/.credentials.json bundle missing 'accessToken'"
        )
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CredentialImportError(
            "~/.claude/.credentials.json bundle missing 'refreshToken'"
        )
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise CredentialImportError(
            "~/.claude/.credentials.json bundle missing integer 'expiresAt'"
        )
    scopes_raw = bundle.get("scopes")
    if isinstance(scopes_raw, list) and all(
        isinstance(s, str) and s for s in scopes_raw
    ) and scopes_raw:
        scopes = list(scopes_raw)
    else:
        # The bundle occasionally omits scopes; fall back to the known
        # consumer scope set so the schema's non-empty-list rule holds.
        scopes = [s for s in CONSUMER_SCOPES.split(" ") if s]
    subscription_type = bundle.get("subscriptionType")
    if not isinstance(subscription_type, str) or not subscription_type:
        subscription_type = None
    return ClaudeDiscovery(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=int(expires_at),
        scopes=scopes,
        subscription_type=subscription_type,
        source_path=path,
    )


# ── Claude: validation + identity ────────────────────────────


@dataclass
class ClaudeIdentity:
    org_uuid: str
    organization_name: str
    account_email: str


def parse_claude_identity(body: Any) -> ClaudeIdentity:
    """Parse a ``GET /api/oauth/profile`` body into a :class:`ClaudeIdentity`.

    The profile response nests ``organization`` and ``account`` objects —
    the same shape the token endpoint returns (see
    :func:`claude_oauth.parse_token_response`). Missing required identity
    raises :class:`CredentialImportError` so the credential is reported
    ``needs-sign-in`` rather than written with a bogus key.
    """
    if not isinstance(body, dict):
        raise CredentialImportError(
            f"profile response is not a JSON object: {type(body).__name__}"
        )
    org = body.get("organization") or {}
    acct = body.get("account") or {}
    org_uuid = org.get("uuid") if isinstance(org, dict) else None
    if not isinstance(org_uuid, str) or not org_uuid:
        raise CredentialImportError(
            "profile response missing 'organization.uuid'"
        )
    # The PROFILE endpoint returns "email"; "email_address" belongs to the
    # TOKEN endpoint's payload (claude_oauth.py) — the two are not the same
    # shape. Accept both, preferring the profile's own field, so this parser
    # is honest about the endpoint it actually calls (host incident
    # 2026-08-14: the fixture had been written to match the code, so the
    # test confirmed the code agreed with itself).
    account_email = None
    if isinstance(acct, dict):
        account_email = acct.get("email") or acct.get("email_address")
    if not isinstance(account_email, str) or not account_email:
        raise CredentialImportError(
            "profile response missing 'account.email'"
        )
    org_name = org.get("name") if isinstance(org, dict) else None
    if not isinstance(org_name, str) or not org_name:
        # Name is display-only; keep the row writable even if the profile
        # omits it by falling back to the email's domain.
        domain = account_email.split("@", 1)[-1]
        org_name = domain or org_uuid
    return ClaudeIdentity(
        org_uuid=org_uuid,
        organization_name=org_name,
        account_email=account_email,
    )


def _http_fetch_claude_identity(access_token: str) -> ClaudeIdentity:
    """Live ``GET /api/oauth/profile`` with the consumer access token.

    The read-only no-op that validates liveness and returns the org
    identity. Never logs the bearer token. Any HTTP / network / parse
    failure raises :class:`CredentialImportError` (the credential is
    reported ``needs-sign-in``).
    """
    req = urllib.request.Request(
        CLAUDE_PROFILE_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": CLAUDE_OAUTH_BETA_HEADER,
            "Content-Type": "application/json",
            "User-Agent": CLAUDE_USER_AGENT,
        },
        method="GET",
    )
    logger.info("credential import: GET %s (Bearer auth)", CLAUDE_PROFILE_URL)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body_bytes = resp.read()
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.error(
            "credential import: profile GET FAILED HTTP %d in %.1fms",
            exc.code, elapsed_ms,
        )
        raise CredentialImportError(
            f"Claude validation failed: profile endpoint returned HTTP {exc.code}"
        ) from None
    except urllib.error.URLError as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.error(
            "credential import: profile GET UNREACHABLE in %.1fms: %s",
            elapsed_ms, exc.reason,
        )
        raise CredentialImportError(
            f"Claude validation failed: profile endpoint unreachable: {exc.reason}"
        ) from None
    elapsed_ms = (time.monotonic() - started) * 1000
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error(
            "credential import: profile GET HTTP %s returned invalid JSON in %.1fms",
            status, elapsed_ms,
        )
        raise CredentialImportError(
            "Claude validation failed: profile endpoint returned invalid JSON"
        ) from None
    identity = parse_claude_identity(body)
    logger.info(
        "credential import: profile OK HTTP %s in %.1fms org=%s",
        status, elapsed_ms, identity.org_uuid,
    )
    return identity


# ── Claude: payload build + import ───────────────────────────


def resolve_alias(
    override: str | None,
    identity: ClaudeIdentity,
    existing_payload: dict[str, Any] | None,
) -> str:
    """Pick the operator-friendly alias for the credentials row.

    Precedence: explicit ``--alias`` → the alias already on the existing
    row (never clobber an operator's chosen name on re-import) → the
    account email's local-part → an ``imported-<org8>`` fallback.
    """
    if override and override.strip():
        return override.strip()
    if existing_payload:
        prior = existing_payload.get("alias")
        if isinstance(prior, str) and prior.strip():
            return prior.strip()
    local = identity.account_email.split("@", 1)[0]
    if local:
        return local
    return f"imported-{identity.org_uuid[:8]}"


def build_claude_payload(
    disc: ClaudeDiscovery,
    identity: ClaudeIdentity,
    *,
    alias: str,
) -> dict[str, Any]:
    """Assemble the ``dashboard.claude.credentials`` payload for a row.

    A write only ever happens with a *fresh* on-disk bundle (see
    :func:`import_claude`'s freshness rule), so the payload is written
    clean — exactly like the install path — carrying no ``last_refresh_*``
    metadata. This is deliberate: preserving a stale
    ``last_refresh_error`` (e.g. a prior ``invalid_grant``) onto a
    newly-valid token would make the refresh poller skip the row as
    revoked and let the fresh bundle go stale. The poller re-stamps
    ``last_refresh_at`` on its next tick.
    """
    return {
        "alias": alias,
        "organization_name": identity.organization_name,
        "account_email": identity.account_email,
        "access_token": disc.access_token,
        "refresh_token": disc.refresh_token,
        "expires_at_ms": disc.expires_at_ms,
        "scopes": list(disc.scopes),
    }


def _read_existing_claude_row(
    org_uuid: str,
    *,
    read_set: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """Return the existing credentials payload for ``org_uuid`` or ``None``.

    Reads from ``personal`` with ``peers=[]`` — exactly how the launcher
    and pollers read — so the freshness comparison sees the same row the
    consumers do.
    """
    reader = read_set or ops.read_set
    try:
        members = reader(
            CLAUDE_CREDENTIALS_SET_ID, org=CREDENTIALS_ORG, peers=[],
        )
    except Exception:  # noqa: BLE001 — set may not exist yet on a fresh DB
        return None
    for member in getattr(members, "members", []) or []:
        if member.key == org_uuid:
            return member.payload if isinstance(member.payload, dict) else {}
    return None


def _write_claude_row(
    org_uuid: str,
    payload: dict[str, Any],
    *,
    upsert: Callable[..., Any] | None = None,
) -> None:
    writer = upsert or ops.upsert_by_key
    writer(
        CLAUDE_CREDENTIALS_SET_ID,
        CLAUDE_CREDENTIALS_REVISION,
        org_uuid,
        payload,
        org=CREDENTIALS_ORG,
    )


def import_claude(
    home: str,
    *,
    alias_override: str | None = None,
    dry_run: bool = False,
    fetch_identity: Callable[[str], ClaudeIdentity] = _http_fetch_claude_identity,
    read_set: Callable[..., Any] | None = None,
    upsert: Callable[..., Any] | None = None,
) -> HarnessResult:
    """Discover → validate → import the local Claude consumer bundle.

    Idempotency / freshness rule: the substrate row is keyed by the org
    UUID (never duplicates). On re-import we only overwrite when the
    on-disk bundle is *newer* than what the substrate holds
    (``expires_at_ms`` comparison). This keeps a second run a no-op
    (``unchanged``) while still letting a fresh local re-auth flow in, and
    critically never regresses a bundle the refresh poller has already
    rotated ahead of the on-disk copy.
    """
    try:
        disc = discover_claude(home)
    except CredentialImportError as exc:
        return HarnessResult("claude", STATUS_NEEDS_SIGN_IN, str(exc))
    if disc is None:
        return HarnessResult(
            "claude", STATUS_NEEDS_SIGN_IN,
            "no ~/.claude/.credentials.json — run `claude` to sign in",
        )
    try:
        identity = fetch_identity(disc.access_token)
    except CredentialImportError as exc:
        return HarnessResult("claude", STATUS_NEEDS_SIGN_IN, str(exc))

    existing = _read_existing_claude_row(identity.org_uuid, read_set=read_set)
    if existing is not None:
        existing_exp = existing.get("expires_at_ms")
        if isinstance(existing_exp, int) and existing_exp >= disc.expires_at_ms:
            return HarnessResult(
                "claude", STATUS_UNCHANGED,
                f"already current for {identity.account_email} "
                f"(org {identity.organization_name})",
                identity.account_email,
            )
    alias = resolve_alias(alias_override, identity, existing)
    payload = build_claude_payload(disc, identity, alias=alias)
    if dry_run:
        verb = "would refresh" if existing is not None else "would import"
        return HarnessResult(
            "claude", STATUS_WOULD_IMPORT,
            f"{verb} {identity.account_email} (org {identity.organization_name}, "
            f"alias {alias!r})",
            identity.account_email,
        )
    _write_claude_row(identity.org_uuid, payload, upsert=upsert)
    verb = "refreshed" if existing is not None else "imported"
    logger.info(
        "credential import: claude %s org=%s alias=%r",
        verb, identity.org_uuid, alias,
    )
    return HarnessResult(
        "claude", STATUS_IMPORTED,
        f"{verb} {identity.account_email} (org {identity.organization_name}, "
        f"alias {alias!r})",
        identity.account_email,
    )


# ── Codex: discovery + validation ────────────────────────────


@dataclass
class CodexDiscovery:
    auth_mode: str | None
    account_id: str | None
    email: str | None
    id_token_exp_ms: int | None
    access_token: str | None
    refresh_token: str | None
    id_token: str | None
    has_api_key: bool
    source_path: str

    @property
    def has_access_token(self) -> bool:
        return bool(self.access_token)

    @property
    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token)


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT payload segment (no signature check).

    Codex's ``id_token`` is a standard JWT; we only read its ``exp`` /
    identity claims, never trust it for authorization. Returns ``{}`` on
    any structural problem.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1]
    padding = "=" * (-len(segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(segment + padding)
        claims = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def discover_codex(home: str) -> CodexDiscovery | None:
    """Parse ``<home>/.codex/auth.json`` into a :class:`CodexDiscovery`.

    Returns ``None`` when absent (not authed — report-and-skip). Raises
    :class:`CredentialImportError` on a present-but-unreadable file.
    """
    path = os.path.join(home, CODEX_AUTH_RELPATH)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialImportError(
            f"~/.codex/auth.json is present but unreadable: {exc}"
        ) from None
    if not isinstance(raw, dict):
        raise CredentialImportError(
            "~/.codex/auth.json is not a JSON object"
        )
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    api_key = raw.get("OPENAI_API_KEY")
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    claims = _decode_jwt_claims(id_token) if isinstance(id_token, str) else {}
    exp = claims.get("exp")
    exp_ms = int(exp) * 1000 if isinstance(exp, int) else None
    email = claims.get("email")
    access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
    refresh_token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    return CodexDiscovery(
        auth_mode=raw.get("auth_mode") if isinstance(raw.get("auth_mode"), str) else None,
        account_id=tokens.get("account_id") if isinstance(tokens, dict) else None,
        email=email if isinstance(email, str) and email else None,
        id_token_exp_ms=exp_ms,
        access_token=access_token if isinstance(access_token, str) and access_token else None,
        refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        id_token=id_token if isinstance(id_token, str) and id_token else None,
        has_api_key=bool(isinstance(api_key, str) and api_key),
        source_path=path,
    )


def validate_codex(disc: CodexDiscovery, *, now_ms: int) -> tuple[bool, str]:
    """Validate a discovered Codex credential. Returns ``(ok, detail)``.

    Codex is consumed differently from Claude: the launcher mounts
    ``auth.json`` read-only and Codex **self-refreshes its access token on
    launch** using the file's ``refresh_token``. So the true liveness gate
    is *refresh capability*, not the id_token's ``exp`` — an expired
    id_token on an account with a live refresh_token still authenticates a
    fresh session. We therefore validate on refresh capability and treat
    the id_token ``exp`` as informational (a stale one just means the
    session token refreshes on next launch).

    * API-key mode (``OPENAI_API_KEY``) → valid; the key proves itself
      when a session calls the API.
    * A ``refresh_token`` present → valid (auto-refresh on launch), with a
      note if the id_token has lapsed.
    * Only an access token, and its id_token has expired, with no refresh
      path → ``needs-sign-in``.
    * No tokens at all → ``needs-sign-in``.

    A network no-op against the OpenAI API would tighten this to true
    liveness; it is deferred (see the module docstring).
    """
    if disc.has_api_key and disc.auth_mode != "chatgpt":
        return True, "OPENAI_API_KEY present"
    if not disc.has_access_token and not disc.has_refresh_token:
        return False, "auth.json has no session tokens — run `codex login`"
    id_token_stale = (
        disc.id_token_exp_ms is not None and disc.id_token_exp_ms <= now_ms
    )
    who = disc.email or disc.account_id or "codex account"
    if disc.has_refresh_token:
        if id_token_stale:
            return True, f"session for {who} (token refreshes on next launch)"
        return True, f"valid session for {who}"
    # access token only, no refresh path
    if id_token_stale:
        return False, "id_token expired and no refresh token — run `codex login`"
    return True, f"valid session for {who}"


def _codex_row_is_writable(disc: CodexDiscovery) -> bool:
    """True when a discovered Codex credential is a keyable OAuth bundle.

    The substrate surface is keyed by ``account_id`` and stores the OAuth
    token triple, so a row is only writable when the ChatGPT-mode bundle
    carries all of them plus the id_token's derived expiry. API-key-mode
    credentials (no ``account_id``, no OAuth tokens) have nothing to store
    on this surface and stay ``in_place`` with no write.
    """
    return bool(
        disc.account_id
        and disc.access_token
        and disc.refresh_token
        and disc.id_token
        and disc.id_token_exp_ms is not None
    )


def build_codex_payload(disc: CodexDiscovery) -> dict[str, Any]:
    """Assemble the ``dashboard.codex.credentials`` payload for a row.

    Written clean — carrying no ``last_refresh_*`` metadata — for the same
    reason the Claude path writes clean: a stale ``last_refresh_error``
    preserved onto a freshly-imported bundle would make the successor
    refresh poller treat the row as revoked. The poller stamps freshness
    on its own ticks. ``auth_mode`` falls back to ``"chatgpt"`` because a
    keyable OAuth bundle is by definition a ChatGPT-mode login even if the
    file omitted the field.
    """
    return {
        "email": disc.email,
        "auth_mode": disc.auth_mode or "chatgpt",
        "access_token": disc.access_token,
        "refresh_token": disc.refresh_token,
        "id_token": disc.id_token,
        "expires_at_ms": disc.id_token_exp_ms,
    }


def _read_existing_codex_row(
    account_id: str,
    *,
    read_set: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """Return the existing Codex credentials payload for ``account_id``.

    Reads from ``personal`` with ``peers=[]`` — exactly how the launcher
    and the successor poller will read — so the freshness comparison sees
    the same row the consumers do.
    """
    reader = read_set or ops.read_set
    try:
        members = reader(
            CODEX_CREDENTIALS_SET_ID, org=CREDENTIALS_ORG, peers=[],
        )
    except Exception:  # noqa: BLE001 — set may not exist yet on a fresh DB
        return None
    for member in getattr(members, "members", []) or []:
        if member.key == account_id:
            return member.payload if isinstance(member.payload, dict) else {}
    return None


def _write_codex_row(
    account_id: str,
    payload: dict[str, Any],
    *,
    upsert: Callable[..., Any] | None = None,
) -> None:
    writer = upsert or ops.upsert_by_key
    writer(
        CODEX_CREDENTIALS_SET_ID,
        CODEX_CREDENTIALS_REVISION,
        account_id,
        payload,
        org=CREDENTIALS_ORG,
    )


def import_codex(
    home: str,
    *,
    now_ms: int | None = None,
    dry_run: bool = False,
    read_set: Callable[..., Any] | None = None,
    upsert: Callable[..., Any] | None = None,
) -> HarnessResult:
    """Discover → validate → import the local Codex credential.

    STEP 1 of the Codex credential end-state (bead auto-kzws9): a validated
    ChatGPT-mode bundle is copied into ``dashboard.codex.credentials``,
    keyed by the ChatGPT ``account_id``, with the same idempotent
    ``upsert_by_key`` + freshness rule the Claude path uses. On re-import we
    only overwrite when the on-disk id_token is *newer* than what the
    substrate holds (``expires_at_ms`` comparison), so a second run is a
    no-op (``unchanged``) and never regresses a bundle a future poller has
    rotated ahead of the on-disk copy.

    TRANSITIONAL: the launcher and the read-only host mount are untouched —
    the credential is written to the substrate *and* still consumed in
    place via the mount until the successor bead cuts the launcher over.
    API-key-mode credentials are not stored here (no ``account_id``, no
    OAuth tokens); they validate and stay ``in_place`` with no write.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        disc = discover_codex(home)
    except CredentialImportError as exc:
        return HarnessResult("codex", STATUS_NEEDS_SIGN_IN, str(exc))
    if disc is None:
        return HarnessResult(
            "codex", STATUS_NEEDS_SIGN_IN,
            "no ~/.codex/auth.json — run `codex login` to sign in",
        )
    ok, detail = validate_codex(disc, now_ms=now)
    account = disc.email or disc.account_id
    if not ok:
        return HarnessResult("codex", STATUS_NEEDS_SIGN_IN, detail, account)

    # API-key mode (and any non-keyable bundle) has no account-keyed row to
    # write — it is validated and consumed in place from the host mount.
    if not _codex_row_is_writable(disc):
        return HarnessResult(
            "codex", STATUS_IN_PLACE,
            f"{detail}; consumed in place from ~/.codex/auth.json",
            account,
        )

    existing = _read_existing_codex_row(disc.account_id, read_set=read_set)
    if existing is not None:
        existing_exp = existing.get("expires_at_ms")
        if isinstance(existing_exp, int) and existing_exp >= disc.id_token_exp_ms:
            return HarnessResult(
                "codex", STATUS_UNCHANGED,
                f"already current for {account} (account {disc.account_id})",
                account,
            )
    payload = build_codex_payload(disc)
    if dry_run:
        verb = "would refresh" if existing is not None else "would import"
        return HarnessResult(
            "codex", STATUS_WOULD_IMPORT,
            f"{verb} {account} (account {disc.account_id})",
            account,
        )
    _write_codex_row(disc.account_id, payload, upsert=upsert)
    verb = "refreshed" if existing is not None else "imported"
    logger.info(
        "credential import: codex %s account=%s", verb, disc.account_id,
    )
    return HarnessResult(
        "codex", STATUS_IMPORTED,
        f"{verb} {account} (account {disc.account_id})",
        account,
    )


# ── orchestration ────────────────────────────────────────────


def run_import(
    home: str,
    *,
    alias_override: str | None = None,
    dry_run: bool = False,
) -> ImportReport:
    """Import both harnesses and collect a per-harness report."""
    report = ImportReport()
    report.add(import_claude(
        home, alias_override=alias_override, dry_run=dry_run,
    ))
    report.add(import_codex(home, dry_run=dry_run))
    return report


# ── CLI ──────────────────────────────────────────────────────


def _format_report(report: ImportReport) -> str:
    lines = []
    for r in report.results:
        marker = {
            STATUS_IMPORTED: "✓",
            STATUS_UNCHANGED: "=",
            STATUS_IN_PLACE: "✓",
            STATUS_WOULD_IMPORT: "·",
            STATUS_NEEDS_SIGN_IN: "○",
        }.get(r.status, "-")
        lines.append(f"  {marker} {r.harness:<6} {r.status:<13} {r.detail}")
    return "\n".join(lines)


def cmd_credentials_import(args: argparse.Namespace) -> None:
    home = os.path.expanduser(args.home) if args.home else os.path.expanduser("~")
    report = run_import(
        home, alias_override=args.alias, dry_run=args.dry_run,
    )
    print("Credential import" + (" (dry run)" if args.dry_run else "") + ":")
    print(_format_report(report))


def attach_credentials_subparser(sub: Any) -> None:
    """Wire ``graph credentials import`` onto the ``graph`` parser."""
    p_cred = sub.add_parser(
        "credentials",
        help=(
            "Layer-0: import the machine's existing Claude/Codex auth into "
            "the substrate credential Settings (outer-agent install step)"
        ),
    )
    cred_sub = p_cred.add_subparsers(dest="credentials_subcmd", required=True)

    p_import = cred_sub.add_parser(
        "import",
        help=(
            "Discover, validate, and copy-import local Claude/Codex "
            "credentials. Consume-only: originals are never modified; no "
            "secret is ever prompted for."
        ),
    )
    p_import.add_argument(
        "--alias",
        default=None,
        help=(
            "Operator-friendly name for the imported Claude account "
            "(defaults to the account email's local-part; an existing "
            "row's alias is never clobbered)."
        ),
    )
    p_import.add_argument(
        "--home",
        default=None,
        help="Override the home directory scanned (default: $HOME).",
    )
    p_import.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be imported without writing anything.",
    )
    p_import.set_defaults(func=cmd_credentials_import)
