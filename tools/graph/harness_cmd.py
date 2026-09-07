"""``graph harness`` — every account the platform can authenticate as.

One screen answering the question you actually have when something stops
working: which accounts exist, are any of them out of headroom, when does that
change, and what is currently running against each.

Harness-agnostic on purpose. Claude and Codex are what exist today; a third
appears here the moment it writes ``dashboard.harness.usage`` rows, with no
change to this command.

Never prints a credential. The values it shows — alias, account, window usage,
reset time, session counts — are the ones you need to diagnose a problem, and
the token itself is never one of them.
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

USAGE_SET_ID = "dashboard.harness.usage"
CREDENTIAL_SETS = {
    "claude": "dashboard.claude.credentials",
    "codex": "dashboard.codex.credentials",
}

_LIVE_STATES = {"live", "running", "active"}
_DEAD_ACTIVITY = {"dead", "ended"}


# ── reading ──────────────────────────────────────────────────


def _api_base() -> str:
    return os.environ.get("GRAPH_API") or "https://localhost:8080"


def _get_json(path: str, *, org: str | None = None, timeout: int = 30) -> Any:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    headers = {"X-Graph-Org": org} if org else {}
    # The dashboard API is auth-gated; send the session bearer like the other
    # graph subcommands (link/worktree/client) do. Without it every request
    # 401s and _members() swallows it into [] — which this command then
    # misreports as "no harness accounts are known".
    token = os.environ.get("CROSSTALK_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{_api_base()}{path}", headers=headers)
    return json.load(urllib.request.urlopen(req, context=ctx, timeout=timeout))


def _members(set_id: str, org: str) -> list[dict]:
    try:
        return _get_json(f"/api/graph/settings/{set_id}", org=org).get("members", [])
    except Exception:
        return []


def _live_session_counts() -> tuple[Counter, bool]:
    """Live sessions per ``(harness, account uuid)``. Second value is False
    when the dashboard could not be reached, so the caller can say so rather
    than print a confident zero.

    Reads ``/api/dao/session_status``, the projection that actually carries
    live sessions. ``/api/dao/recent_sessions`` deliberately does NOT: its
    handler describes a bounded card projection the Sessions page reads once
    *behind* the already-rendered Active list, with everything after that
    coming from lifecycle SSE. Counting from it returned agentic runs and
    half-populated stub rows whose ``harness`` is null, which is where the
    ``None:None`` key in ``--json`` came from.

    Sessions are keyed by ``harness_token`` -- the Anthropic organization
    UUID of the credential the launcher picked -- because that is what the
    usage rows are keyed by. A session with no recorded token is counted
    under ``None`` and reported separately rather than being attributed to
    an account that may not have served it.
    """
    try:
        data = _get_json("/api/dao/session_status", timeout=40)
    except Exception:
        return Counter(), False
    rows = data.get("rows") if isinstance(data, dict) else data
    counts: Counter = Counter()
    for row in rows or []:
        if str(row.get("state") or "").upper() != "ACTIVE":
            continue
        if str(row.get("activity_state") or "").lower() in _DEAD_ACTIVITY:
            continue
        harness = str(row.get("harness") or "").strip().lower() or None
        counts[(harness, row.get("harness_token") or None)] += 1
    return counts, True


# ── formatting ───────────────────────────────────────────────


def _fmt_reset(resets_at: Any, now: int) -> str:
    if not isinstance(resets_at, int):
        return "—"
    remaining = resets_at - now
    if remaining <= 0:
        return "due"
    hours, minutes = divmod(remaining // 60, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _fmt_window(window: Any, now: int) -> str:
    if not isinstance(window, dict):
        return "—"
    used = window.get("used_percent")
    if not isinstance(used, (int, float)):
        return "—"
    return f"{float(used):5.1f}% ({_fmt_reset(window.get('resets_at'), now)})"


def _verdict(payload: dict, now: int) -> str:
    """What this account can do right now, in one word.

    Deliberately no "stale" verdict. A window that has just rolled reports
    zero used and carries no reset time, because there is nothing pending to
    reset -- the freshest state an account can be in, not the least
    trustworthy. How current the reading is shows in the SEEN column, where a
    reader can weigh it themselves.
    """
    from tools.dashboard.harness_usage_settings import is_exhausted

    if is_exhausted(payload, now_epoch=now):
        return "EXHAUSTED"
    if payload.get("status") not in (None, "ok"):
        return "unknown"
    return "ok"


def _age(updated_at: Any) -> str:
    if not isinstance(updated_at, str) or not updated_at:
        return "—"
    try:
        from datetime import datetime, timezone
        when = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        seconds = int((datetime.now(timezone.utc) - when).total_seconds())
    except Exception:
        return "—"
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    return f"{minutes // 60}h"


# ── the command ──────────────────────────────────────────────


def cmd_harness_status(args) -> None:
    now = int(time.time())
    # No org by default. These sets are @home("personal"), and the dashboard
    # resolves a personal-homed row for any authenticated caller; a container
    # seat that names an org it is not a member of is refused outright
    # ("organization mismatch: the request's bearer and X-Graph-Org name
    # different organizations"), which is why this command used to report
    # "no harness accounts are known" from every session.
    org = getattr(args, "org", None)

    usage_rows = _members(USAGE_SET_ID, org)
    session_counts, dashboard_reachable = _live_session_counts()

    aliases: dict[str, dict] = {}
    for harness, set_id in CREDENTIAL_SETS.items():
        for member in _members(set_id, org):
            payload = member.get("payload") or {}
            alias = payload.get("alias")
            if alias:
                aliases[(harness, alias)] = payload

    # account uuid -> operator-facing alias, for the LIVE column and --json.
    alias_by_account = {
        (payload or {}).get("account_id"): (payload or {}).get("alias")
        for payload in ((m.get("payload") or {}) for m in usage_rows)
        if payload.get("account_id") and payload.get("alias")
    }

    if getattr(args, "json", False):
        print(json.dumps({
            "usage": [m.get("payload") for m in usage_rows],
            "live_sessions": {
                f"{h or 'unknown'}:{alias_by_account.get(t) or t or 'unattributed'}": n
                for (h, t), n in session_counts.items()
            },
            "dashboard_reachable": dashboard_reachable,
        }, indent=2))
        return

    if not usage_rows:
        print("  no harness accounts are known — run `graph claude install`")
        return

    print(f"  {'HARNESS':8s} {'ACCOUNT':16s} {'STATE':10s} "
          f"{'SHORT WINDOW':20s} {'LONG WINDOW':20s} {'SEEN':5s} {'LIVE':4s}")
    print("  " + "─" * 92)

    exhausted: list[str] = []
    for member in sorted(usage_rows, key=lambda m: (
            (m.get("payload") or {}).get("harness") or "",
            (m.get("payload") or {}).get("alias") or "")):
        payload = member.get("payload") or {}
        harness = payload.get("harness") or "?"
        alias = payload.get("alias") or payload.get("identity_label") or "—"
        windows = payload.get("windows") or {}
        state = _verdict(payload, now)
        live = session_counts.get((harness, payload.get("account_id")), 0)
        print(f"  {harness:8s} {str(alias)[:16]:16s} {state:10s} "
              f"{_fmt_window(windows.get('short'), now):20s} "
              f"{_fmt_window(windows.get('long'), now):20s} "
              f"{_age(payload.get('updated_at')):5s} "
              f"{live if dashboard_reachable else '?':>4}")
        if state == "EXHAUSTED":
            resets = None
            for window in windows.values():
                if isinstance(window, dict) and isinstance(window.get("resets_at"), int):
                    resets = window["resets_at"] if resets is None else min(
                        resets, window["resets_at"])
            exhausted.append(f"{harness}/{alias} (frees in {_fmt_reset(resets, now)})")

        credential = aliases.get((harness, alias))
        if credential and credential.get("last_refresh_error"):
            print(f"  {'':8s} └─ refresh error: "
                  f"{str(credential['last_refresh_error'])[:70]}")

    # A session whose launch recorded no credential cannot be attributed to
    # an account. Say so rather than let its absence read as an idle account.
    attributable = {
        (m.get("payload") or {}).get("harness")
        for m in usage_rows if (m.get("payload") or {}).get("account_id")
    }
    for (harness, token), count in sorted(
        session_counts.items(), key=lambda item: str(item[0][0]),
    ):
        if token is None and harness in attributable:
            print(f"\n  ! {count} live {harness} session(s) are not attributed "
                  f"to an account: no credential was recorded at launch")

    if not dashboard_reachable:
        print("\n  ! the dashboard is not reachable, so live session counts are unknown")
    if exhausted:
        print("\n  ! out of headroom, and not selectable while it lasts:")
        for line in exhausted:
            print(f"      {line}")
        print("    a session started now uses another account, unless it is the "
              "only one installed")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "harness",
        help="Show every account the platform can authenticate as, with usage",
    )
    parser.add_argument(
        "--org", default=None,
        help="Route the read through this organization. Rarely needed: the "
             "rows are personal-homed and the dashboard resolves them for "
             "any authenticated caller.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.set_defaults(func=cmd_harness_status)
