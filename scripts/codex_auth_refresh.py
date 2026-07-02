#!/usr/bin/env python3
"""Headless refresh for Codex (ChatGPT-plan) OAuth tokens in ~/.codex/auth.json.

Codex authenticates against a ChatGPT account via OAuth and stores an
`access_token` (~10-day TTL) plus a long-lived, *rotating* `refresh_token`
in `~/.codex/auth.json`. The CLI normally keeps itself logged in by
rewriting that file — but agent containers mount it **read-only**, so on the
host nothing rotates the token on its own. It lapses, and every codex
container fails at once (pitfall graph://a2792dae-f33).

This script does the refresh **headlessly** (no browser): it POSTs the
`refresh_token` to the OpenAI token endpoint, receives rotated tokens, and
writes them back into `auth.json` **in place** so read-only bind-mounted
containers observe the new bytes without a restart. Intended to run from a
systemd --user timer (see ~/.config/systemd/user/codex-auth-refresh.*).

Canonical Codex auth doc: see the Codex Authentication signpost in the graph.
Proven refresh recipe origin: graph://a2792dae-f33 (comment, 2026-05-31).

Exit codes:
  0  refreshed, or already fresh (nothing to do)
  2  refresh_token revoked  -> operator must run `codex login --device-auth`
  3  transient (HTTP 429 / network / timeout) -> timer retries next tick
  1  usage / unexpected error
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_SCOPE = "openid profile email"
# Refresh once the access_token has less than this much life left. With a
# daily timer and a ~10-day TTL, this rotates the token roughly weekly —
# comfortably ahead of expiry, and keeps the refresh_token exercised.
REFRESH_THRESHOLD_S = 4 * 24 * 3600  # 4 days


def _claims(jwt: str) -> dict:
    """Decode a JWT payload (claims) without verifying the signature."""
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _load() -> dict:
    with open(AUTH_PATH) as f:
        return json.load(f)


def _access_exp(auth: dict) -> int | None:
    tok = auth.get("tokens", {}).get("access_token")
    try:
        return int(_claims(tok).get("exp")) if tok else None
    except Exception:
        return None


def _client_id(auth: dict) -> str | None:
    tok = auth.get("tokens", {}).get("access_token", "")
    try:
        return _claims(tok).get("client_id")
    except Exception:
        return None


def _plan(auth: dict) -> str | None:
    tok = auth.get("tokens", {}).get("id_token", "")
    try:
        return _claims(tok).get("https://api.openai.com/auth", {}).get("chatgpt_plan_type")
    except Exception:
        return None


def _refresh(auth: dict) -> dict:
    """POST the refresh_token; return the token response JSON (raises on HTTP error)."""
    tokens = auth.get("tokens", {})
    body = json.dumps({
        "client_id": _client_id(auth),
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "scope": DEFAULT_SCOPE,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _write_back(auth: dict, tok: dict) -> None:
    """Merge rotated tokens into auth and overwrite auth.json IN PLACE.

    In-place (O_TRUNC on the existing inode) rather than write-temp-then-rename:
    the file is bind-mounted read-only into containers, so a rename would swap
    the inode and containers would keep seeing the stale file. Truncating the
    existing inode keeps the mount valid and preserves 0600 perms.
    """
    dst = auth.setdefault("tokens", {})
    for k in ("access_token", "id_token", "refresh_token"):
        if tok.get(k):
            dst[k] = tok[k]
    auth["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    data = json.dumps(auth, indent=2).encode()
    fd = os.open(AUTH_PATH, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless Codex OAuth token refresh.")
    ap.add_argument("--force", action="store_true", help="refresh regardless of remaining lifetime")
    ap.add_argument("--dry-run", action="store_true", help="report the decision; no POST, no write")
    ap.add_argument("--status", action="store_true", help="print current token status and exit")
    args = ap.parse_args()

    if not os.path.exists(AUTH_PATH):
        print(f"no {AUTH_PATH} -- run: codex login --device-auth", file=sys.stderr)
        return 2

    auth = _load()
    exp = _access_exp(auth)
    remaining = (exp - int(time.time())) if exp else None
    plan = _plan(auth)

    if args.status or args.dry_run:
        exp_h = datetime.fromtimestamp(exp, timezone.utc).isoformat() if exp else "?"
        rem_h = f"{remaining // 3600}h" if remaining is not None else "?"
        print(f"plan={plan} access_exp={exp_h} remaining={rem_h} last_refresh={auth.get('last_refresh')}")
        if args.dry_run:
            need = args.force or (remaining is not None and remaining < REFRESH_THRESHOLD_S)
            print(f"would_refresh={need}  threshold={REFRESH_THRESHOLD_S // 3600}h  client_id={_client_id(auth)}")
        return 0

    if not (args.force or (remaining is not None and remaining < REFRESH_THRESHOLD_S)):
        print(f"fresh: {remaining // 3600}h remaining (> {REFRESH_THRESHOLD_S // 3600}h) -- no refresh")
        return 0

    try:
        resp = _refresh(auth)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        if e.code in (400, 401) and "invalid_grant" in detail:
            print(f"refresh_token revoked ({e.code} invalid_grant) -- run: codex login --device-auth", file=sys.stderr)
            return 2
        print(f"transient HTTP {e.code}: {detail}", file=sys.stderr)
        return 3
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"transient network error: {e}", file=sys.stderr)
        return 3

    if not all(resp.get(k) for k in ("access_token", "refresh_token")):
        print(f"unexpected token response (missing fields): keys={list(resp)}", file=sys.stderr)
        return 1

    _write_back(auth, resp)
    new = _load()
    new_exp = _access_exp(new)
    print("refreshed OK: plan={} new_access_exp={}".format(
        _plan(new),
        datetime.fromtimestamp(new_exp, timezone.utc).isoformat() if new_exp else "?",
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
