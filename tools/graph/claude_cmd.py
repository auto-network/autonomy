"""``graph claude`` subcommand group — operator surface for substrate-stored
Claude account credentials.

Sub-commands implemented (graph://73c4e9ef-bbc):

* ``graph claude install --alias <name>`` — interactive: drives consumer
  OAuth + console OAuth + setup-token mint; writes both substrate rows.
* ``graph claude install --alias <name> --refresh-setup-token`` — re-mint
  only path; replaces the setup-token row in place.
* ``graph claude list`` — table of installed accounts.
* ``graph claude usage`` — table of per-account 5h/7d window usage.
* ``graph claude remove --alias <name>`` — confirms + deletes both rows.

The OAuth + PKCE machinery itself lives in :mod:`tools.graph.claude_oauth`
so tests can mock at the helper boundary; this module owns the CLI shape
and the substrate writes.

A core invariant of install: the consumer flow's organization UUID must
match the console flow's. We abort with a clear "you logged in as
different accounts the second time" error before writing anything when
they don't.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

from . import ops
from .claude_oauth import (
    CONSOLE_SCOPES,
    CONSUMER_SCOPES,
    FlowResult,
    OAuthError,
    TokenResponse,
    mint_setup_token,
    run_oauth_flow,
)
from .schemas.claude_credentials import (
    CLAUDE_CREDENTIALS_REVISION,
    CLAUDE_CREDENTIALS_SET_ID,
)
from .schemas.claude_setup_tokens import (
    CLAUDE_SETUP_TOKEN_TTL,
    CLAUDE_SETUP_TOKENS_REVISION,
    CLAUDE_SETUP_TOKENS_SET_ID,
)


logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


def _print_table(rows: list[dict], cols: list[tuple[str, str, int]]) -> None:
    """Print a table with ``cols`` = (key, header, width) tuples."""
    header = "  ".join(f"{h:<{w}}" for _, h, w in cols)
    sep = "  ".join("─" * w for _, _, w in cols)
    print(header)
    print(sep)
    for r in rows:
        line = "  ".join(
            f"{str(r.get(k, '') or '')[:w]:<{w}}" for k, _, w in cols
        )
        print(line)


def _format_ms_timestamp(epoch_ms: int | None) -> str:
    if not epoch_ms:
        return "-"
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M")


def _format_iso(value: str | None) -> str:
    if not value:
        return "-"
    return str(value)[:19].replace("T", " ").rstrip("Z").rstrip()


def _setup_token_expires_at(setup_row: Any | None) -> str | None:
    """Compute substrate ``expires_at`` for a setup-token row.

    ``ResolvedSetting`` does not expose ``expires_at`` directly today, so
    we derive it from ``created_at + CLAUDE_SETUP_TOKEN_TTL`` — the same
    arithmetic the @cache decorator stamps at write time, so the answer
    matches what the substrate row carries.
    """
    if setup_row is None:
        return None
    created_at = getattr(setup_row, "created_at", None)
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(
            str(created_at).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    return (dt + CLAUDE_SETUP_TOKEN_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_credentials_rows() -> list[Any]:
    """Return the list of ``ResolvedSetting`` rows for installed credentials."""
    members = ops.read_set(
        CLAUDE_CREDENTIALS_SET_ID, org=ops.CALLER_ORG,
    )
    return list(members.members)


def _read_setup_token_rows() -> list[Any]:
    members = ops.read_set(
        CLAUDE_SETUP_TOKENS_SET_ID, org=ops.CALLER_ORG,
    )
    return list(members.members)


def _credentials_by_alias(alias: str) -> Any | None:
    for m in _read_credentials_rows():
        payload = m.payload if isinstance(m.payload, dict) else {}
        if payload.get("alias") == alias:
            return m
    return None


def _credentials_by_org_uuid(org_uuid: str) -> Any | None:
    for m in _read_credentials_rows():
        if m.key == org_uuid:
            return m
    return None


def _setup_token_by_org_uuid(org_uuid: str) -> Any | None:
    for m in _read_setup_token_rows():
        if m.key == org_uuid:
            return m
    return None


# ── install ──────────────────────────────────────────────────


def _build_credentials_payload(
    *, alias: str, token: TokenResponse, last_refresh_at: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "alias": alias,
        "organization_name": token.organization_name,
        "account_email": token.account_email,
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "expires_at_ms": _now_ms() + (token.expires_in * 1000),
        "scopes": [s for s in (token.scope or "").split(" ") if s],
    }
    if last_refresh_at is not None:
        payload["last_refresh_at"] = last_refresh_at
    return payload


def _write_credentials_row(*, org_uuid: str, payload: dict[str, Any]) -> str:
    """Idempotent write of the credentials row for ``org_uuid``.

    Re-running install with the same alias updates the row in place via
    ``upsert_by_key`` — a key collision on the org UUID means we already
    track this account, so we should rotate the bundle, not error.
    """
    return ops.upsert_by_key(
        CLAUDE_CREDENTIALS_SET_ID,
        CLAUDE_CREDENTIALS_REVISION,
        org_uuid,
        payload,
        org=ops.CALLER_ORG,
    )


def _write_setup_token_row(*, org_uuid: str, raw_key: str) -> str:
    """Idempotent write of the setup-token row for ``org_uuid``.

    Re-mint paths replace the existing row in place; substrate
    ``expires_at`` is restamped to ``created_at + 1y`` on each
    upsert (the @cache decorator's TTL drives that).
    """
    return ops.upsert_by_key(
        CLAUDE_SETUP_TOKENS_SET_ID,
        CLAUDE_SETUP_TOKENS_REVISION,
        org_uuid,
        {"raw_key": raw_key},
        org=ops.CALLER_ORG,
    )


def _alias_collision_check(alias: str, org_uuid: str) -> None:
    """Refuse to repurpose an alias that is already pointing at a different
    Anthropic org. Re-running with the same (alias, org_uuid) is fine.
    """
    existing = _credentials_by_alias(alias)
    if existing is None:
        return
    if existing.key == org_uuid:
        return
    print(
        f"Error: alias {alias!r} is already installed and points at a "
        f"different Anthropic org ({existing.key}). To re-target the alias "
        f"to a new account, remove it first: "
        f"`graph claude remove --alias {alias}`.",
        file=sys.stderr,
    )
    sys.exit(1)


def _run_consumer_flow() -> FlowResult:
    print("Step 1/2: Claude consumer login (browser will open)…")
    return run_oauth_flow(scope=CONSUMER_SCOPES)


def _run_console_flow() -> FlowResult:
    print(
        "Step 2/2: Claude console login (browser will open) — same account "
        "as step 1…"
    )
    return run_oauth_flow(scope=CONSOLE_SCOPES)


def _do_install_full(args: argparse.Namespace) -> int:
    """Drive the full install: consumer flow + console flow + mint + writes."""
    logger.info("claude install: starting full install alias=%r", args.alias)
    try:
        consumer = _run_consumer_flow()
    except OAuthError as e:
        logger.error("claude install: consumer flow failed alias=%r: %s", args.alias, e)
        print(f"Error during consumer login: {e}", file=sys.stderr)
        return 1

    org_uuid = consumer.token.organization_uuid
    logger.info(
        "claude install: consumer flow OK alias=%r org=%s account=%s",
        args.alias, org_uuid, consumer.token.account_email,
    )
    _alias_collision_check(args.alias, org_uuid)

    try:
        console = _run_console_flow()
    except OAuthError as e:
        logger.error("claude install: console flow failed alias=%r: %s", args.alias, e)
        print(f"Error during console login: {e}", file=sys.stderr)
        return 1

    if console.token.organization_uuid != org_uuid:
        logger.error(
            "claude install: same-org check failed — consumer org=%s console org=%s",
            org_uuid, console.token.organization_uuid,
        )
        print(
            "Error: you logged in as different accounts the second time "
            f"({consumer.token.account_email} vs "
            f"{console.token.account_email}). Aborting before any writes.",
            file=sys.stderr,
        )
        return 1
    logger.info("claude install: console flow OK alias=%r org=%s (same-org confirmed)",
                args.alias, org_uuid)

    try:
        raw_key = mint_setup_token(
            console_access_token=console.token.access_token,
        )
    except OAuthError as e:
        logger.error("claude install: mint failed alias=%r org=%s: %s",
                     args.alias, org_uuid, e)
        print(f"Error minting setup token: {e}", file=sys.stderr)
        return 1

    payload = _build_credentials_payload(
        alias=args.alias, token=consumer.token, last_refresh_at=None,
    )
    _write_credentials_row(org_uuid=org_uuid, payload=payload)
    _write_setup_token_row(org_uuid=org_uuid, raw_key=raw_key)
    logger.info(
        "claude install: substrate writes OK alias=%r org=%s (credentials + setup_tokens)",
        args.alias, org_uuid,
    )

    print(
        f"Installed Claude account: alias={args.alias!r} "
        f"org={consumer.token.organization_name!r} "
        f"account={consumer.token.account_email!r}"
    )
    return 0


def _do_install_refresh_setup_token(args: argparse.Namespace) -> int:
    """Skip the consumer flow; only run the console flow + mint, replace the
    setup-token row in place. Used when the year-long token is about to
    expire or has been revoked.
    """
    logger.info("claude install: --refresh-setup-token alias=%r", args.alias)
    existing = _credentials_by_alias(args.alias)
    if existing is None:
        logger.error(
            "claude install: --refresh-setup-token alias=%r — no existing credentials row",
            args.alias,
        )
        print(
            f"Error: --refresh-setup-token requires an existing install for "
            f"alias {args.alias!r}; run `graph claude install --alias "
            f"{args.alias}` first.",
            file=sys.stderr,
        )
        return 1
    expected_org_uuid = existing.key
    try:
        console = _run_console_flow()
    except OAuthError as e:
        logger.error(
            "claude install: --refresh-setup-token alias=%r console flow failed: %s",
            args.alias, e,
        )
        print(f"Error during console login: {e}", file=sys.stderr)
        return 1
    if console.token.organization_uuid != expected_org_uuid:
        logger.error(
            "claude install: --refresh-setup-token alias=%r same-org check failed "
            "(expected=%s console=%s)",
            args.alias, expected_org_uuid, console.token.organization_uuid,
        )
        print(
            f"Error: console login resolved to org "
            f"{console.token.organization_uuid!r}, but alias {args.alias!r} "
            f"is bound to org {expected_org_uuid!r}. Log in as the same "
            "account, or remove the alias and re-install.",
            file=sys.stderr,
        )
        return 1
    try:
        raw_key = mint_setup_token(
            console_access_token=console.token.access_token,
        )
    except OAuthError as e:
        logger.error(
            "claude install: --refresh-setup-token alias=%r org=%s mint failed: %s",
            args.alias, expected_org_uuid, e,
        )
        print(f"Error minting setup token: {e}", file=sys.stderr)
        return 1
    _write_setup_token_row(org_uuid=expected_org_uuid, raw_key=raw_key)
    logger.info(
        "claude install: --refresh-setup-token alias=%r org=%s OK (setup_tokens row replaced)",
        args.alias, expected_org_uuid,
    )
    print(
        f"Refreshed setup token for alias={args.alias!r} "
        f"org={expected_org_uuid!r}"
    )
    return 0


def cmd_claude_install(args: argparse.Namespace) -> None:
    if not args.alias or not args.alias.strip():
        print(
            "Error: --alias is required and must be a non-empty string",
            file=sys.stderr,
        )
        sys.exit(1)
    args.alias = args.alias.strip()
    if args.refresh_setup_token:
        rc = _do_install_refresh_setup_token(args)
    else:
        rc = _do_install_full(args)
    if rc != 0:
        sys.exit(rc)


# ── list ─────────────────────────────────────────────────────


def cmd_claude_list(args: argparse.Namespace) -> None:  # noqa: ARG001
    creds = _read_credentials_rows()
    setup_tokens = {m.key: m for m in _read_setup_token_rows()}
    if not creds:
        print("(no Claude accounts installed — run `graph claude install`)")
        return
    rows: list[dict[str, str]] = []
    for m in creds:
        payload = m.payload if isinstance(m.payload, dict) else {}
        st = setup_tokens.get(m.key)
        # ``updated_at`` on the credentials row tracks the last time the
        # row was written — install or refresh. ``last_refresh_error``
        # comes from the refresh poller (separate bead) and is empty
        # until that lands.
        bundle_refreshed = (
            payload.get("last_refresh_at")
            or getattr(m, "updated_at", None)
        )
        # The setup-token row's substrate ``expires_at`` is the year
        # the row was minted + 1y; computed from ``created_at + 1y``
        # to match what the @cache decorator stamps at write time.
        token_expires_at = _setup_token_expires_at(st)
        rows.append({
            "alias": payload.get("alias", "-"),
            "org": payload.get("organization_name", "-"),
            "email": payload.get("account_email", "-"),
            "bundle_refreshed": _format_iso(bundle_refreshed),
            "token_expires": _format_iso(token_expires_at),
            "last_error": payload.get("last_refresh_error", "") or "",
        })
    _print_table(rows, [
        ("alias", "ALIAS", 14),
        ("org", "ORG", 22),
        ("email", "ACCOUNT EMAIL", 28),
        ("bundle_refreshed", "BUNDLE REFRESHED", 19),
        ("token_expires", "SETUP-TOKEN EXP", 19),
        ("last_error", "LAST REFRESH ERROR", 30),
    ])


# ── usage ────────────────────────────────────────────────────


def _read_harness_usage_rows() -> list[Any]:
    """Read all ``dashboard.harness.usage`` members (every harness, every key).

    Filtering to ``harness == 'claude'`` happens in the caller — the
    harness-usage rows for codex are written under the same set_id and we
    only want claude rows for ``graph claude usage``.
    """
    try:
        members = ops.read_set(
            "dashboard.harness.usage", org=ops.CALLER_ORG,
        )
    except Exception:  # noqa: BLE001 — set may not exist yet on a fresh DB
        return []
    return list(members.members)


def _format_window_pct(window: dict[str, Any] | None) -> str:
    if not isinstance(window, dict):
        return "-"
    used = window.get("used_percent")
    if used is None:
        return "-"
    try:
        return f"{float(used):.0f}%"
    except (TypeError, ValueError):
        return "-"


def _format_resets_at(short: dict[str, Any] | None) -> str:
    if not isinstance(short, dict):
        return "-"
    epoch = short.get("resets_at")
    if not epoch:
        return "-"
    try:
        dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M")


def cmd_claude_usage(args: argparse.Namespace) -> None:  # noqa: ARG001
    creds_by_org = {m.key: m for m in _read_credentials_rows()}
    if not creds_by_org:
        print("(no Claude accounts installed — run `graph claude install`)")
        return
    usage_by_org: dict[str, dict[str, Any]] = {}
    for m in _read_harness_usage_rows():
        payload = m.payload if isinstance(m.payload, dict) else {}
        if payload.get("harness") != "claude":
            continue
        account_id = payload.get("account_id")
        if isinstance(account_id, str) and account_id:
            usage_by_org[account_id] = payload
    rows: list[dict[str, str]] = []
    for org_uuid, cred in creds_by_org.items():
        cpayload = cred.payload if isinstance(cred.payload, dict) else {}
        usage = usage_by_org.get(org_uuid) or {}
        windows = usage.get("windows") or {}
        rows.append({
            "alias": cpayload.get("alias", "-"),
            "five_h": _format_window_pct(windows.get("short")),
            "seven_d": _format_window_pct(windows.get("long")),
            "resets_at": _format_resets_at(windows.get("short")),
            "freshness": _format_iso(usage.get("updated_at")) if usage else "-",
        })
    _print_table(rows, [
        ("alias", "ALIAS", 14),
        ("five_h", "5H", 6),
        ("seven_d", "7D", 6),
        ("resets_at", "5H RESETS", 19),
        ("freshness", "SOURCE FRESHNESS", 19),
    ])


# ── remove ───────────────────────────────────────────────────


def cmd_claude_remove(args: argparse.Namespace) -> None:
    if not args.alias or not args.alias.strip():
        print(
            "Error: --alias is required and must be a non-empty string",
            file=sys.stderr,
        )
        sys.exit(1)
    alias = args.alias.strip()
    cred = _credentials_by_alias(alias)
    if cred is None:
        print(f"No installed Claude account with alias {alias!r}.")
        return
    org_uuid = cred.key
    cpayload = cred.payload if isinstance(cred.payload, dict) else {}
    org_name = cpayload.get("organization_name", "(unknown)")
    account_email = cpayload.get("account_email", "(unknown)")
    if not args.yes:
        print(
            f"About to remove Claude account: alias={alias!r} "
            f"org={org_name!r} email={account_email!r}."
        )
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.")
            return
    logger.info("claude remove: deleting alias=%r org=%s", alias, org_uuid)
    try:
        ops.remove_setting(cred.id, org=ops.CALLER_ORG)
    except Exception as e:  # noqa: BLE001 — surface to operator
        logger.error("claude remove: credentials row delete failed alias=%r org=%s: %s",
                     alias, org_uuid, e)
        print(f"Error removing credentials row: {e}", file=sys.stderr)
        sys.exit(1)
    setup = _setup_token_by_org_uuid(org_uuid)
    if setup is not None:
        try:
            ops.remove_setting(setup.id, org=ops.CALLER_ORG)
        except Exception as e:  # noqa: BLE001
            logger.error("claude remove: setup_token row delete failed alias=%r org=%s: %s",
                         alias, org_uuid, e)
            print(f"Error removing setup-token row: {e}", file=sys.stderr)
            sys.exit(1)
    logger.info("claude remove: alias=%r org=%s OK (both rows deleted)", alias, org_uuid)
    print(f"Removed Claude account: alias={alias!r}.")


# ── argparse wiring ──────────────────────────────────────────


def attach_claude_subparser(sub: Any) -> None:
    """Wire up ``graph claude ...`` subcommands onto the ``graph`` parser."""
    p_claude = sub.add_parser(
        "claude",
        help=(
            "Manage Claude account credentials in graph settings "
            "(graph://73c4e9ef-bbc)"
        ),
    )
    claude_sub = p_claude.add_subparsers(dest="claude_subcmd", required=True)

    p_install = claude_sub.add_parser(
        "install",
        help=(
            "Run the consumer + console OAuth flows and write the credentials "
            "and setup-token rows. With --refresh-setup-token, only re-mint."
        ),
    )
    p_install.add_argument(
        "--alias", required=True,
        help=(
            "Operator-friendly free-form name (e.g. 'gmail-max'). Stored on "
            "the credentials row's payload."
        ),
    )
    p_install.add_argument(
        "--refresh-setup-token", action="store_true",
        dest="refresh_setup_token",
        help=(
            "Skip the consumer flow; only run the console flow + mint, "
            "replacing the setup-token row in place. Used when the year-long "
            "token is about to expire or has been revoked."
        ),
    )
    p_install.set_defaults(func=cmd_claude_install)

    p_list = claude_sub.add_parser(
        "list",
        help="Show installed Claude accounts (alias, org, email, freshness).",
    )
    p_list.set_defaults(func=cmd_claude_list)

    p_usage = claude_sub.add_parser(
        "usage",
        help="Show 5h / 7d window usage per installed account.",
    )
    p_usage.set_defaults(func=cmd_claude_usage)

    p_remove = claude_sub.add_parser(
        "remove",
        help="Delete the credentials and setup-token rows for an alias.",
    )
    p_remove.add_argument(
        "--alias", required=True,
        help="Alias of the account to remove.",
    )
    p_remove.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt.",
    )
    p_remove.set_defaults(func=cmd_claude_remove)
