"""``graph claude`` subcommand group — operator surface for substrate-stored
Claude account credentials.

Sub-commands:

* ``graph claude install`` — prints the two ways an account gets in: ``graph
  credentials import`` for a sign-in already on this machine, and ``claude
  setup-token`` for a one-year token (graph://5ab13dd5-570). The dashboard
  runs no OAuth flow of its own and never refreshes a Claude sign-in
  (auto-n9tdh, operator ruling 2026-10-02).
* ``graph claude list`` — table of installed accounts.
* ``graph claude usage`` — table of per-account 5h/7d window usage.
* ``graph claude remove --alias <name>`` — confirms + deletes the account.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from . import harness_credentials as hv
from . import ops


logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────


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


def _setup_token_expires_at(acct: hv.Account | None) -> str | None:
    """When the account's setup token expires: minted-at plus one year."""
    if acct is None or acct.get("setup") is None:
        return None
    minted = hv.parse_iso(acct.get("setup_minted_at"))
    if minted is None:
        return None
    return (minted + hv.SETUP_TOKEN_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")


def _credentials_org() -> str:
    """The org that owns every row this module touches, for a DIRECT read.

    The accounts live in the operator's vault (record v16 §10.9) and the
    usage rows in ``dashboard.harness.usage``; both are operator-local:
    their correct value depends on *this* machine, and they sync only
    across the operator's own fleet. Their home is ``personal`` (org-scope
    rubric graph://4d88c2ad-625, worked-examples table).

    This names that home for the ``--force-host`` path, which bypasses the
    dashboard and reads the local database directly. Every other read goes
    through :func:`_read_rows`, which asks the dashboard scopelessly and lets
    the server resolve the home -- the same law stated the other way round:
    *never read a setting through the process's ambient ``GRAPH_ORG``*.

    Passing ``ops.CALLER_ORG`` here would follow ``GRAPH_ORG``. Agent shells
    set ``GRAPH_ORG=autonomy``, so ``graph claude list`` read a different
    database than the running system wrote, and reported a two-month-stale
    row carrying ``invalid_grant`` for an account that was refreshing
    normally -- a health surface that failed toward false alarm.
    """
    return "personal"


def _read_rows(set_id: str) -> list[Any]:
    """Members of a personal-homed set, from whichever seat is asking.

    Two seats, one rule, and it is the branch ``client.py`` already
    documents: with ``GRAPH_API`` set we are in a container and route
    through the dashboard; without it we are on the host (or in a test) and
    read the local database directly.

    Over HTTP the request carries NO ``X-Graph-Org``, so the server resolves
    the row's declared home instead of taking a scope from the caller. A
    container seat that names ``personal`` explicitly is refused --
    "organization mismatch: the request's bearer and X-Graph-Org name
    different organizations" -- which is why ``graph claude list`` and
    ``graph claude usage`` printed "no Claude accounts installed" from every
    session while the dashboard was serving those very rows. Reading the
    container's own ``personal.db`` instead is no better: it is empty,
    because these rows live on the host.

    Locally the home has to be named: see :func:`_credentials_org` for why
    it must not be the ambient ``GRAPH_ORG``.
    """
    if os.environ.get("GRAPH_API"):
        from .client import get_client
        members = get_client().read_set(set_id, org=None, peers=[])
    else:
        members = ops.read_set(set_id, org=_credentials_org(), peers=[])
    return list(members.members)


def _accounts(org: str | None = None) -> list[hv.Account]:
    """Every installed Claude account, from the vault (record v16 §10.9), or
    with *org* that organization's shared accounts (auto-26e8a)."""
    return hv.list_accounts("claude", org=org)


def _account_by_alias(alias: str, org: str | None = None) -> hv.Account | None:
    for acct in _accounts(org):
        if acct.get("alias") == alias:
            return acct
    return None


def _account_by_org_uuid(org_uuid: str, org: str | None = None) -> hv.Account | None:
    return hv.read_account("claude", org_uuid, org=org)


# ── install ──────────────────────────────────────────────────


def cmd_claude_install(args: argparse.Namespace) -> None:  # noqa: ARG001
    """The two routes by which a Claude account gets into the vault."""
    print(
        "graph claude install does not sign in. A Claude account gets in one of\n"
        "two ways:\n"
        "  1. An existing sign-in on this machine (claude logged in):\n"
        "       graph credentials import\n"
        "  2. A one-year setup token, minted with `claude setup-token`; see\n"
        "       graph://5ab13dd5-570\n"
    )


# ── list ─────────────────────────────────────────────────────


def cmd_claude_list(args: argparse.Namespace) -> None:
    accounts = _accounts(getattr(args, "org", None))
    if not accounts:
        print("(no Claude accounts installed — run `graph claude install`)")
        return
    rows: list[dict[str, str]] = []
    for acct in accounts:
        rows.append({
            "alias": acct.get("alias") or "-",
            "org": acct.get("org_name") or "-",
            "email": acct.get("email") or "-",
            "bundle_refreshed": _format_iso(acct.get("refreshed_at")),
            "token_expires": _format_iso(_setup_token_expires_at(acct)),
            "last_error": acct.get("error") or "",
        })
    _print_table(rows, [
        ("alias", "ALIAS", 14),
        ("org", "ORG", 22),
        ("email", "ACCOUNT EMAIL", 28),
        ("bundle_refreshed", "BUNDLE REFRESHED", 19),
        ("token_expires", "SETUP-TOKEN EXP", 19),
        ("last_error", "LAST REFRESH ERROR", 30),
    ])


def _read_harness_usage_rows() -> list[Any]:
    """Read all ``dashboard.harness.usage`` members (every harness, every key).

    Filtering to ``harness == 'claude'`` happens in the caller — the
    harness-usage rows for codex are written under the same set_id and we
    only want claude rows for ``graph claude usage``.
    """
    try:
        return _read_rows("dashboard.harness.usage")
    except Exception:  # noqa: BLE001 — set may not exist yet on a fresh DB
        return []


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
    accounts = _accounts()
    if not accounts:
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
    for acct in accounts:
        usage = usage_by_org.get(acct.id) or {}
        windows = usage.get("windows") or {}
        rows.append({
            "alias": acct.get("alias") or "-",
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
    org = getattr(args, "org", None)
    acct = _account_by_alias(alias, org)
    if acct is None:
        print(f"No installed Claude account with alias {alias!r}.")
        return
    org_name = acct.get("org_name") or "(unknown)"
    account_email = acct.get("email") or "(unknown)"
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
    logger.info("claude remove: deleting alias=%r org=%s", alias, acct.id)
    try:
        removed = hv.remove_account("claude", acct.id, org=org)
    except Exception as e:  # noqa: BLE001 — surface to operator
        logger.error("claude remove: delete failed alias=%r org=%s: %s", alias, acct.id, e)
        print(f"Error removing the account: {e}", file=sys.stderr)
        sys.exit(1)
    logger.info("claude remove: alias=%r org=%s OK (%d rows deleted)", alias, acct.id, removed)
    print(f"Removed Claude account: alias={alias!r}.")


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
        help=("Print how a Claude account gets in: `graph credentials import` "
              "for a sign-in on this machine, or `claude setup-token` for a "
              "one-year token (graph://5ab13dd5-570)."),
    )
    p_install.set_defaults(func=cmd_claude_install)

    p_list = claude_sub.add_parser(
        "list",
        help="Show installed Claude accounts (alias, org, email, freshness).",
    )
    p_list.add_argument(
        "--org", default=None,
        help="Act on this organization's shared accounts instead of your own vault.",
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
    p_remove.add_argument(
        "--org", default=None,
        help="Act on this organization's shared accounts instead of your own vault.",
    )
    p_remove.set_defaults(func=cmd_claude_remove)
