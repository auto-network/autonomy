"""Host-side OAuth refresh poller for ``dashboard.claude.credentials`` rows.

Containers and the harness-usage poller can't refresh the consumer-scope
OAuth bundle themselves — Anthropic rotates the refresh_token on every
exchange and containers mount the substrate read-only. The host runs
this single tick every hour to keep every row's ``access_token`` (and
the rotated ``refresh_token``) ahead of the 8h Anthropic expiry.

Bead 3 of 4 in the substrate-stored Claude credentials cutover. Spec:
``graph://73c4e9ef-bbc``.

Failure handling:

* HTTP 401 / ``invalid_grant`` → refresh_token has been revoked. We
  stamp ``last_refresh_error = "invalid_grant: …"`` on the row, leave
  the existing tokens untouched (so the harness-usage poller keeps
  whatever read access it still has), and log a clear "re-run
  ``graph claude install --alias <alias>``" message. Subsequent ticks
  skip the row until the next operator-initiated install rotates it.
* HTTP 429 / network / timeout → transient. Stamp
  ``last_refresh_error = "<short>"`` and try again next tick.

On success we always rotate both tokens (Anthropic returns a new
refresh_token on every refresh) and clear ``last_refresh_error``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tools.graph import ops as graph_ops
from tools.graph.claude_oauth import CLIENT_ID, TOKEN_URL
from tools.graph.schemas.claude_credentials import (
    CLAUDE_CREDENTIALS_REVISION,
    CLAUDE_CREDENTIALS_SET_ID,
)


logger = logging.getLogger(__name__)


# 1h between ticks. Token TTL is ~8h; with a 4h "needs refresh" threshold
# every active row lands in the refresh window once per ~4h.
CREDENTIAL_REFRESH_POLL_INTERVAL = 3600.0

# Refresh when remaining lifetime drops to or below 4h.
CREDENTIAL_FRESH_THRESHOLD_MS = 4 * 60 * 60 * 1000

# Sentinel prefix on ``last_refresh_error`` that means the refresh_token
# is gone. Operator-initiated install via ``graph claude install`` will
# overwrite the row through ``upsert_by_key`` (which doesn't include
# ``last_refresh_error`` in its install payload), naturally clearing
# this state.
INVALID_GRANT_PREFIX = "invalid_grant"


@dataclass
class RefreshResult:
    """Outcome of one refresh attempt for a single credentials row."""

    kind: str  # "ok" | "revoked" | "transient"
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    error: str | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _credentials_org() -> str:
    return (
        os.environ.get("GRAPH_ORG")
        or os.environ.get("GRAPH_SCOPE")
        or "autonomy"
    )


# ── HTTP shim ────────────────────────────────────────────────


def _post_refresh(refresh_token: str) -> tuple[int, dict[str, Any]]:
    """POST to the token endpoint; return ``(status, parsed_body)``.

    The token endpoint accepts JSON for the refresh grant (proven in the
    design note's probe — same shape Claude Code's own refresh path uses).
    On a captured ``HTTPError`` we still parse the error body so the caller
    can read ``error`` / ``error_description`` for classification. Network
    errors propagate so the caller's ``except`` can route them as transient.
    """
    encoded = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body_bytes = resp.read()
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read() or b""
        status = exc.code
    try:
        body = json.loads(body_bytes.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    return status, body


# ── Pure classification helpers ──────────────────────────────


def refresh_one(refresh_token: str) -> RefreshResult:
    """Hit the OAuth token endpoint with ``refresh_token``; classify the result.

    Returns a :class:`RefreshResult` with ``kind`` ∈ {``ok``, ``revoked``,
    ``transient``}. Validation failures on a 2xx body fall under
    ``transient`` — we don't have enough to write tokens, but the next
    tick may succeed.
    """
    try:
        status, body = _post_refresh(refresh_token)
    except urllib.error.URLError as exc:
        return RefreshResult(kind="transient", error=f"network: {exc.reason}")
    except Exception as exc:  # noqa: BLE001 — surface unexpected as transient
        return RefreshResult(
            kind="transient",
            error=f"{type(exc).__name__}: {exc}",
        )
    if 200 <= status < 300:
        access = body.get("access_token")
        refresh = body.get("refresh_token")
        expires = body.get("expires_in")
        if not isinstance(access, str) or not access:
            return RefreshResult(
                kind="transient", error="response missing access_token"
            )
        if not isinstance(refresh, str) or not refresh:
            return RefreshResult(
                kind="transient", error="response missing refresh_token"
            )
        if not isinstance(expires, int) or expires <= 0:
            return RefreshResult(
                kind="transient", error="response missing/invalid expires_in"
            )
        return RefreshResult(
            kind="ok",
            access_token=access,
            refresh_token=refresh,
            expires_in=int(expires),
        )
    err_code = str(body.get("error") or "")
    err_desc = str(body.get("error_description") or "")
    short = (err_desc or err_code or f"HTTP {status}").strip()
    if status == 401 or err_code == "invalid_grant":
        prefix = INVALID_GRANT_PREFIX
        suffix = short
        msg = f"{prefix}: {suffix}" if suffix and suffix != prefix else prefix
        return RefreshResult(kind="revoked", error=msg)
    return RefreshResult(kind="transient", error=f"HTTP {status}: {short}")


def _row_payload(row: Any) -> dict[str, Any]:
    payload = getattr(row, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _is_revoked(payload: dict[str, Any]) -> bool:
    err = payload.get("last_refresh_error")
    return isinstance(err, str) and err.startswith(INVALID_GRANT_PREFIX)


def _needs_refresh(payload: dict[str, Any], *, now_ms: int) -> bool:
    """``True`` when the row's access_token is within the refresh window.

    Missing / non-int ``expires_at_ms`` is treated as "needs refresh" so
    a row written by an older code path doesn't silently never refresh.
    """
    expires = payload.get("expires_at_ms")
    if not isinstance(expires, int):
        return True
    return (expires - now_ms) <= CREDENTIAL_FRESH_THRESHOLD_MS


def _build_payload_after_refresh(
    *, base: dict[str, Any], result: RefreshResult, now_ms: int, now_iso: str,
) -> dict[str, Any]:
    """Produce the upsert payload for a row given a refresh ``result``.

    Always starts from ``base`` (the existing row payload) so required
    fields (alias, organization_name, account_email, scopes) survive.
    On success we rotate both tokens, advance ``expires_at_ms`` and
    ``last_refresh_at``, and drop ``last_refresh_error``. On failure we
    leave the tokens untouched and stamp the error.
    """
    payload = dict(base)
    if result.kind == "ok":
        assert result.access_token and result.refresh_token and result.expires_in
        payload["access_token"] = result.access_token
        payload["refresh_token"] = result.refresh_token
        payload["expires_at_ms"] = now_ms + result.expires_in * 1000
        payload["last_refresh_at"] = now_iso
        payload.pop("last_refresh_error", None)
    else:
        payload["last_refresh_error"] = result.error or "unknown"
    # Optional fields stored as ``None`` would round-trip but the schema
    # accepts both forms; prefer omission so rows stay tidy.
    if payload.get("last_refresh_at") is None:
        payload.pop("last_refresh_at", None)
    if payload.get("last_refresh_error") is None:
        payload.pop("last_refresh_error", None)
    return payload


# ── Per-row + tick orchestration ─────────────────────────────


def refresh_credential_row(
    row: Any, *, org: str, now_ms: int, now_iso: str,
) -> RefreshResult | None:
    """Refresh ``row`` if it's stale; return the result or ``None`` when skipped.

    Skips when:
    * the row's ``last_refresh_error`` already marks it as
      ``invalid_grant`` (operator must re-install),
    * the row has plenty of TTL left (``expires_at_ms - now_ms > 4h``), or
    * the row is missing a usable ``refresh_token``.
    """
    payload = _row_payload(row)
    if _is_revoked(payload):
        return None
    if not _needs_refresh(payload, now_ms=now_ms):
        return None
    refresh_tok = payload.get("refresh_token")
    if not isinstance(refresh_tok, str) or not refresh_tok:
        return None
    result = refresh_one(refresh_tok)
    new_payload = _build_payload_after_refresh(
        base=payload, result=result, now_ms=now_ms, now_iso=now_iso,
    )
    graph_ops.upsert_by_key(
        CLAUDE_CREDENTIALS_SET_ID,
        CLAUDE_CREDENTIALS_REVISION,
        row.key,
        new_payload,
        org=org,
    )
    if result.kind == "revoked":
        alias = payload.get("alias") or "?"
        org_name = payload.get("organization_name") or row.key
        # graph_ops.upsert_by_key already wrote the error onto the row
        # so `graph claude list` surfaces it; the WARNING gives an
        # operator who's tailing the dashboard log the actionable
        # remediation in one line.
        logger.warning(
            "claude credentials refresh: invalid_grant for org=%r alias=%r — "
            "re-run `graph claude install --alias %s`",
            org_name, alias, alias,
        )
    elif result.kind == "transient":
        alias = payload.get("alias") or "?"
        logger.warning(
            "claude credentials refresh: transient failure for alias=%r: %s",
            alias, result.error,
        )
    return result


def refresh_all_credentials() -> dict[str, int]:
    """Iterate every ``dashboard.claude.credentials`` row and refresh as needed.

    Returns counters keyed by ``ok`` / ``revoked`` / ``transient`` /
    ``skipped``. Caller usually just logs the dict.
    """
    org = _credentials_org()
    counters: dict[str, int] = {
        "ok": 0, "revoked": 0, "transient": 0, "skipped": 0,
    }
    try:
        members = graph_ops.read_set(
            CLAUDE_CREDENTIALS_SET_ID, org=org,
        )
    except Exception:
        logger.exception(
            "claude credentials refresh: read_set failed; tick aborted",
        )
        return counters
    rows = list(getattr(members, "members", []) or [])
    if not rows:
        return counters
    now_ms = _now_ms()
    now_iso = _now_iso()
    for row in rows:
        try:
            result = refresh_credential_row(
                row, org=org, now_ms=now_ms, now_iso=now_iso,
            )
        except Exception:
            logger.exception(
                "claude credentials refresh: row %r failed",
                getattr(row, "key", "?"),
            )
            counters["transient"] += 1
            continue
        if result is None:
            counters["skipped"] += 1
        else:
            counters[result.kind] = counters.get(result.kind, 0) + 1
    return counters


# ── Background poller ────────────────────────────────────────


def should_run_credentials_refresh_poller() -> bool:
    """Mirror the harness-usage poller's environment gate.

    Mock mode and pytest both skip — the same write paths run inline in
    those environments via direct calls to :func:`refresh_all_credentials`
    when needed.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


async def credentials_refresh_poller() -> None:
    """Background asyncio task — fire :func:`refresh_all_credentials` every
    :data:`CREDENTIAL_REFRESH_POLL_INTERVAL` seconds.

    Logs the first tick on entry and each tick's counters so operators
    can see the loop is alive in ``data/dashboard.log``.
    """
    logger.info(
        "claude credentials refresh poller started (interval=%ss)",
        CREDENTIAL_REFRESH_POLL_INTERVAL,
    )
    while True:
        try:
            counters = await asyncio.to_thread(refresh_all_credentials)
            logger.info("claude credentials refresh tick: %s", counters)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("claude credentials refresh poller tick failed")
        await asyncio.sleep(CREDENTIAL_REFRESH_POLL_INTERVAL)
